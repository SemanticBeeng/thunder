"""Classes relating to ImageBlocks, the default / reference implementation of an Images partitioning strategy.
"""
import cStringIO as StringIO
import itertools
import struct
from numpy import arange, expand_dims, zeros
from thunder.rdds.data import Data
from thunder.rdds.keys import Dimensions
from thunder.rdds.images import PartitioningStrategy, PartitionedImages, PartitioningKey


class ImageBlocksPartitioningStrategy(PartitioningStrategy):
    """A PartitioningStrategy that groups Images into nonoverlapping, roughly equally-sized blocks.

    The number and dimensions of image blocks are specified as "splits per dimension", which is for each
    spatial dimension of the original Images the number of partitions to generate along that dimension. So
    for instance, given a 12 x 12 Images object, an ImageBlocksPartitioningStrategy with splitsPerDim=(2,2)
    would yield PartitionedImages with 4 blocks, each 6 x 6.
    """
    def __init__(self, splitsPerDim, numSparkPartitions=None):
        """Returns a new ImageBlocksPartitioningStrategy.

        Parameters
        ----------
        splitsPerDim : n-tuple of positive int, where n = dimensionality of image
            Specifies that intermediate blocks are to be generated by splitting the i-th dimension
            of the image into splitsPerDim[i] roughly equally-sized partitions.
            1 <= splitsPerDim[i] <= self.dims[i]
        """
        super(ImageBlocksPartitioningStrategy, self).__init__()
        self._splitsPerDim = ImageBlocksPartitioningStrategy.__normalizeSplits(splitsPerDim)
        self._slices = None
        self._npartitions = reduce(lambda x, y: x * y, self._splitsPerDim, 1) if not numSparkPartitions \
            else int(numSparkPartitions)

    def getPartitionedImagesClass(self):
        return ImageBlocks

    @classmethod
    def generateFromBlockSize(cls, blockSize, dims, nimages, datatype, numSparkPartitions=None, **kwargs):
        """Returns a new ImageBlocksPartitioningStrategy, that yields blocks
        closely matching the requested size in bytes.

        Parameters
        ----------
        blockSize : positive int or string
            Requests an average size for the intermediate blocks in bytes. A passed string should
            be in a format like "256k" or "150M" (see util.common.parseMemoryString). If blocksPerDim
            or groupingDim are passed, they will take precedence over this argument. See
            imageblocks._BlockMemoryAsSequence for a description of the partitioning strategy used.

        Returns
        -------
        n-tuple of positive int, where n == len(self.dims)
            Each value in the returned tuple represents the number of splits to apply along the
            corresponding dimension in order to yield blocks close to the requested size.
        """
        import bisect
        from numpy import dtype
        from thunder.utils.common import parseMemoryString
        minseriessize = nimages * dtype(datatype).itemsize

        if isinstance(blockSize, basestring):
            blockSize = parseMemoryString(blockSize)

        memseq = _BlockMemoryAsReversedSequence(dims)
        tmpidx = bisect.bisect_left(memseq, blockSize / float(minseriessize))
        if tmpidx == len(memseq):
            # handle case where requested block is bigger than the biggest image
            # we can produce; just give back the biggest block size
            tmpidx -= 1
        splitsPerDim = memseq.indtosub(tmpidx)
        return cls(splitsPerDim, numSparkPartitions=numSparkPartitions, **kwargs)

    @property
    def npartitions(self):
        """The number of Spark partitions across which the resulting RDD is to be distributed.
        """
        return self._npartitions

    @staticmethod
    def __normalizeSplits(splitsPerDim):
        splitsPerDim = map(int, splitsPerDim)
        if any((nsplits <= 0 for nsplits in splitsPerDim)):
            raise ValueError("All numbers of blocks must be positive; got " + str(splitsPerDim))
        return splitsPerDim

    def __validateSplitsForImage(self):
        dims = self.dims
        splitsPerDim = self._splitsPerDim
        ndim = len(dims)
        if not len(splitsPerDim) == ndim:
            raise ValueError("splitsPerDim length (%d) must match image dimensionality (%d); " %
                             (len(splitsPerDim), ndim) +
                             "have splitsPerDim %s and image shape %s" % (str(splitsPerDim), str(dims)))

    @staticmethod
    def __generateSlices(splitsPerDim, dims):
        # slices will be sequence of sequences of slices
        # slices[i] will hold slices for ith dimension
        slices = []
        for nsplits, dimsize in zip(splitsPerDim, dims):
            blocksize = dimsize / nsplits  # integer division
            blockrem = dimsize % nsplits
            st = 0
            dimslices = []
            for blockidx in xrange(nsplits):
                en = st + blocksize
                if blockrem:
                    en += 1
                    blockrem -= 1
                dimslices.append(slice(st, min(en, dimsize), 1))
                st = en
            slices.append(dimslices)
        return slices

    def setImages(self, images):
        super(ImageBlocksPartitioningStrategy, self).setImages(images)
        self.__validateSplitsForImage()
        self._slices = ImageBlocksPartitioningStrategy.__generateSlices(self._splitsPerDim, self.dims)

    @staticmethod
    def extractBlockFromImage(imgary, blockslices, timepoint, numtimepoints):
        # add additional "time" dimension onto front of val
        val = expand_dims(imgary[blockslices], axis=0)
        origshape = [numtimepoints] + list(imgary.shape)
        origslices = [slice(timepoint, timepoint+1, 1)] + list(blockslices)
        return ImageBlocksGroupingKey(origshape, origslices), val

    def partitionFunction(self, timePointIdxAndImageArray):
        tpidx, imgary = timePointIdxAndImageArray
        totnumimages = self.nimages
        slices = self._slices

        ret_vals = []
        sliceproduct = itertools.product(*slices)
        for blockslices in sliceproduct:
            # blockval = self._partitionArray(imgary, blockslices)
            # blockval = blockval.addDimension(newdimidx=tpidx, newdimsize=totnumimages)
            ## resulting key will be (x, y, z) (for 3d data), where x, y, z are starting
            ## position of block within image volume
            #newkey = [sl.start for sl in blockslices]
            #ret_vals.append((tuple(newkey), blockval))
            ret_vals.append(ImageBlocksPartitioningStrategy.extractBlockFromImage(imgary, blockslices, tpidx, totnumimages))
        return ret_vals

    def blockingFunction(self, spatialIdxAndPartitionedSequence):
        _, partitionedSequence = spatialIdxAndPartitionedSequence
        # sequence will be of (partitioning key, np array) pairs
        ary = None
        firstkey = None
        for key, block in partitionedSequence:
            if ary is None:
                # set up collection array:
                newshape = [key.origshape[0]] + list(block.shape)[1:]
                ary = zeros(newshape, block.dtype)
                firstkey = key

            # put values into collection array:
            targslices = [key.origslices[0]] + ([slice(None)] * (block.ndim - 1))
            ary[targslices] = block

        # new slices should be full slice for formerly planar dimension, plus existing block slices
        neworigslices = [slice(None)] + list(firstkey.origslices)[1:]
        return ImageBlocksGroupingKey(origshape=firstkey.origshape, origslices=neworigslices), ary


class ImageBlocksGroupingKey(PartitioningKey):
    def __init__(self, origshape, origslices):
        self.origshape = origshape
        self.origslices = origslices

    @property
    def temporalKey(self):
        # temporal key is index of time point, obtained from first slice (time dimension)
        return self.origslices[0].start

    @property
    def spatialKey(self):
        # should this be reversed?
        return tuple(sl.start for sl in self.origslices[1:])

    def __repr__(self):
        return "ImageBlocksGroupingKey(origshape=%s, origslices=%s)" % (self.origshape, self.origslices)


class ImageBlocks(PartitionedImages):
    """Intermediate representation used in conversion from Images to Series.

    This class is not expected to be directly used by clients.
    """
    _metadata = Data._metadata + ['_dims', '_nimages']

    @property
    def _constructor(self):
        return ImageBlocks

    def populateParamsFromFirstRecord(self):
        record = super(ImageBlocks, self).populateParamsFromFirstRecord()
        self._dims = Dimensions.fromTuple(record[0].origshape)
        return record

    @staticmethod
    def toSeriesIter(partitioningkey, ary):
        """Returns an iterator over key,array pairs suitable for casting into a Series object.

        Returns:
        --------
        iterator< key, series >
        key: tuple of int
        series: 1d array of self.values.dtype
        """
        rangeiters = ImageBlocks._get_range_iterators(partitioningkey.origslices, partitioningkey.origshape)
        # remove iterator over temporal dimension where we are requesting series
        del rangeiters[0]
        insertDim = 0

        for idxSeq in itertools.product(*reversed(rangeiters)):
            expandedIdxSeq = list(reversed(idxSeq))
            expandedIdxSeq.insert(insertDim, None)
            slices = []
            for d, (idx, origslice) in enumerate(zip(expandedIdxSeq, partitioningkey.origslices)):
                if idx is None:
                    newslice = slice(None)
                else:
                    # correct slice into our own value for any offset given by origslice:
                    start = idx - origslice.start if not origslice == slice(None) else idx
                    newslice = slice(start, start+1, 1)
                slices.append(newslice)

            series = ary[slices].squeeze()
            yield tuple(reversed(idxSeq)), series

    @staticmethod
    def _get_range_iterators(slices, shape):
        """Returns a sequence of iterators over the range of the slices in self.origslices

        When passed to itertools.product, these iterators should cover the original image
        volume represented by this block.
        """
        iters = []
        for sliceidx, sl in enumerate(slices):
            start = sl.start if not sl.start is None else 0
            stop = sl.stop if not sl.stop is None else shape[sliceidx]
            step = sl.step if not sl.step is None else 1
            it = xrange(start, stop, step)
            iters.append(it)
        return iters

    def toSeries(self):
        from thunder.rdds.series import Series
        # returns generator of (z, y, x) array data for all z, y, x
        seriesrdd = self.rdd.flatMap(lambda kv: ImageBlocks.toSeriesIter(kv[0], kv[1]))

        idx = arange(self._nimages) if self._nimages else None
        return Series(seriesrdd, dims=self.dims, index=idx, dtype=self.dtype)

    def toImages(self, seriesDim=0):
        from thunder.rdds.images import Images
        timerdd = self.rdd.flatMap(lambda (k, v): v.toPlanarBlocks(planarDim=seriesDim))
        squeezedrdd = timerdd.mapValues(lambda v: v.removeDimension(squeezeDim=seriesDim))
        timesortedrdd = squeezedrdd.groupByKey().sortByKey()
        imagesrdd = timesortedrdd.mapValues(self._valuetype.toArray)
        return Images(imagesrdd, dims=self._dims, nimages=self._nimages, dtype=self._dtype)

    @staticmethod
    def getBinarySeriesNameForKey(blockKey):
        """

        Returns
        -------
        string blocklabel
            Block label will be in form "key02_0000k-key01_0000j-key00_0000i" for i,j,k x,y,z indicies as first series
            in block. No extension (e.g. ".bin") is appended to the block label.
        """
        return '-'.join(reversed(["key%02d_%05g" % (ki, k) for (ki, k) in enumerate(blockKey)]))

    def toBinarySeries(self):

        def blockToBinarySeries(kv):
            blockKey, blockVal = kv
            # # blockKey here is in numpy order (reversed from series convention)
            # # reverse again to get correct filename, for correct sorting of files downstream
            # label = ImageBlocks.getBinarySeriesNameForKey(reversed(blockKey))
            label = ImageBlocks.getBinarySeriesNameForKey(blockKey.spatialKey)+".bin"
            keypacker = None
            buf = StringIO.StringIO()
            for seriesKey, series in ImageBlocks.toSeriesIter(blockKey, blockVal):
                if keypacker is None:
                    keypacker = struct.Struct('h'*len(seriesKey))
                # print >> sys.stderr, seriesKey, series, series.tostring().encode('hex')
                buf.write(keypacker.pack(*seriesKey))
                buf.write(series.tostring())
            val = buf.getvalue()
            buf.close()
            return label, val

        return self.rdd.map(blockToBinarySeries)


class _BlockMemoryAsSequence(object):
    """Helper class used in calculation of slices for requested block partitions of a particular size.

    The partitioning strategy represented by objects of this class is to split into N equally-sized
    subdivisions along each dimension, starting with the rightmost dimension.

    So for instance consider an Image with spatial dimensions 5, 10, 3 in x, y, z. The first nontrivial
    partition would be to split into 2 blocks along the z axis:
    splits: (1, 1, 2)
    In this example, downstream this would turn into two blocks, one of size (5, 10, 2) and another
    of size (5, 10, 1).

    The next partition would be to split into 3 blocks along the z axis, which happens to
    corresponding to having a single block per z-plane:
    splits: (1, 1, 3)
    Here these splits would yield 3 blocks, each of size (5, 10, 1).

    After this the z-axis cannot be partitioned further, so the next partition starts splitting along
    the y-axis:
    splits: (1, 2, 3)
    This yields 6 blocks, each of size (5, 5, 1).

    Several other splits are possible along the y-axis, going from (1, 2, 3) up to (1, 10, 3).
    Following this we move on to the x-axis, starting with splits (2, 10, 3) and going up to
    (5, 10, 3), which is the finest subdivision possible for this data.

    Instances of this class represent the average size of a block yielded by this partitioning
    strategy in a linear order, moving from the most coarse subdivision (1, 1, 1) to the finest
    (x, y, z), where (x, y, z) are the dimensions of the array being partitioned.

    This representation is intended to support binary search for the partitioning strategy yielding
    a block size closest to a requested amount.
    """
    def __init__(self, dims):
        self._dims = dims

    def indtosub(self, idx):
        """Converts a linear index to a corresponding partition strategy, represented as
        number of splits along each dimension.
        """
        dims = self._dims
        ndims = len(dims)
        sub = [1] * ndims
        for didx, d in enumerate(dims[::-1]):
            didx = ndims - (didx + 1)
            delta = min(dims[didx]-1, idx)
            if delta > 0:
                sub[didx] += delta
                idx -= delta
            if idx <= 0:
                break
        return tuple(sub)

    def blockMemoryForSplits(self, sub):
        """Returns the average number of cells in a block generated by the passed sequence of splits.
        """
        from operator import mul
        sz = [d / float(s) for (d, s) in zip(self._dims, sub)]
        return reduce(mul, sz)

    def __len__(self):
        return sum([d-1 for d in self._dims]) + 1

    def __getitem__(self, item):
        sub = self.indtosub(item)
        return self.blockMemoryForSplits(sub)


class _BlockMemoryAsReversedSequence(_BlockMemoryAsSequence):
    """A version of _BlockMemoryAsSequence that represents the linear ordering of splits in the
    opposite order, starting with the finest partitioning allowable for the array dimensions.

    This can yield a sequence of block sizes in increasing order, which is required for binary
    search using python's 'bisect' library.
    """
    def _reverseIdx(self, idx):
        l = len(self)
        if idx < 0 or idx >= l:
            raise IndexError("list index out of range")
        return l - (idx + 1)

    def indtosub(self, idx):
        return super(_BlockMemoryAsReversedSequence, self).indtosub(self._reverseIdx(idx))
