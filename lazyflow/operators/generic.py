###############################################################################
#   lazyflow: data flow based lazy parallel computation framework
#
#       Copyright (C) 2011-2014, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the Lesser GNU General Public License
# as published by the Free Software Foundation; either version 2.1
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# See the files LICENSE.lgpl2 and LICENSE.lgpl3 for full text of the
# GNU Lesser General Public License version 2.1 and 3 respectively.
# This information is also available on the ilastik web site at:
# 		   http://ilastik.org/license/
###############################################################################
# Python
import copy
import logging

logger = logging.getLogger(__name__)

# SciPy
import numpy
import vigra

# lazyflow
from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow import roi
from lazyflow.roi import roiToSlice, sliceToRoi, TinyVector, getIntersection, InvalidRoiException
from lazyflow.request import RequestPool

from typing import Tuple


# Utility functions
def axisTagsToString(axistags):
    res = []
    for axistag in axistags:
        res.append(axistag.key)
    return res


def popFlagsFromTheKey(key, axistags, flags):
    d = dict(list(zip(axisTagsToString(axistags), key)))

    newKey = []
    for flag in axisTagsToString(axistags):
        if flag not in flags:
            slice = d[flag]
            newKey.append(slice)

    return newKey


class OpMultiArraySlicer2(Operator):
    """
    Produces a list of image slices along the given axis.
    Same as the slicer operator above, but does not reduce the dimensionality of the data.
    The output image shape will have a dimension of 1 for the axis that was sliced.
    """

    # FIXME: This operator return a singleton in the sliced direction
    # Should be integrated with the above one to have a more consistent notation

    Input = InputSlot()  # The volume to slice up.
    AxisFlag = InputSlot()  # An axis key (e.g. 't' or 'c', etc.), which indicates the axis to slice across

    SliceIndexes = InputSlot(optional=True)  # A list of output slices to actually produce.
    # If provided, ONLY the slices at these specific indexes will be provided on the output.
    # For example, if SliceIndexes.setvalue([2,4,6]), then len(Slices) == 3, and only the
    #  data for slices 2,4,6 will be produced (on Slices[0], Slices[1], Slices[2], respectively)

    Slices = OutputSlot(level=1)

    name = "Multi Array Slicer"
    category = "Misc"

    def __init__(self, *args, **kwargs):
        super(OpMultiArraySlicer2, self).__init__(*args, **kwargs)
        self.inputShape = None

    def setupOutputs(self):
        flag = self.AxisFlag.value

        indexAxis = self.Input.meta.axistags.index(flag)
        inshape = list(self.Input.meta.shape)
        outshape = list(inshape)
        outshape.pop(indexAxis)
        outshape.insert(indexAxis, 1)
        outshape = tuple(outshape)

        outaxistags = copy.copy(self.Input.meta.axistags)

        sliceIndexes = self.getSliceIndexes()
        self.Slices.resize(len(sliceIndexes))

        for oslot in self.Slices:
            # Output metadata is a modified copy of the input's metadata
            oslot.meta.assignFrom(self.Input.meta)
            oslot.meta.axistags = outaxistags
            oslot.meta.shape = outshape
            if self.Input.meta.drange is not None:
                oslot.meta.drange = self.Input.meta.drange

    def getSliceIndexes(self):
        if self.SliceIndexes.ready():
            return self.SliceIndexes.value
        else:
            # Default is all indexes of the sliced axis
            flag = self.AxisFlag.value
            axistags = self.Input.meta.axistags
            indexAxis = axistags.index(flag)
            inshape = self.Input.meta.shape
            return list(range(inshape[indexAxis]))

    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start, rroi.stop)
        index = subindex[0]
        # Index of the input slice this data will come from.
        sliceIndex = self.getSliceIndexes()[index]

        outshape = self.Slices[index].meta.shape
        start, stop = roi.sliceToRoi(key, outshape)

        start = list(start)
        stop = list(stop)

        flag = self.AxisFlag.value
        indexAxis = self.Input.meta.axistags.index(flag)

        start.pop(indexAxis)
        stop.pop(indexAxis)

        start.insert(indexAxis, sliceIndex)
        stop.insert(indexAxis, sliceIndex + 1)

        newKey = roi.roiToSlice(numpy.array(start), numpy.array(stop))

        self.Input[newKey].writeInto(result).wait()
        return result

    def propagateDirty(self, inputSlot, subindex, roi):
        if inputSlot == self.AxisFlag or inputSlot == self.SliceIndexes:
            # AxisFlag or slice set changed.  Everything is dirty
            for i, slot in enumerate(self.Slices):
                slot.setDirty(slice(None))
        elif inputSlot == self.Input:
            # Mark each of the intersected slices as dirty
            sliced_axis = self.Input.meta.axistags.index(self.AxisFlag.value)
            dirty_slice_indexes = list(zip(roi.start, roi.stop))[sliced_axis]

            all_output_slices_indexes = self.getSliceIndexes()
            for i in range(*dirty_slice_indexes):
                if i in all_output_slices_indexes:
                    if i < len(self.Slices):
                        slot = self.Slices[i]
                        sliceRoi = copy.copy(roi)
                        sliceRoi.start[sliced_axis] = 0
                        sliceRoi.stop[sliced_axis] = 1
                        slot.setDirty(sliceRoi)
        else:
            assert False, "Unknown dirty input slot."


class OpMultiArrayStacker(Operator):
    Images = InputSlot(level=1)
    AxisFlag = InputSlot()
    AxisIndex = InputSlot(optional=True)
    Output = OutputSlot()

    name = "Multi Array Stacker"
    description = "Stack inputs on any axis, including the ones which are not there yet"
    category = "Misc"

    def setupOutputs(self):
        # This function is needed so that we don't depend on the order of connections.
        # If axis flag or axis index is connected after the input images, the shape is calculated
        # here
        self.setRightShape()

    def setRightShape(self):
        c = 0
        flag = self.inputs["AxisFlag"].value
        self.intervals = []

        inTagKeys = []

        for inSlot in self.inputs["Images"]:
            inTagKeys = [ax.key for ax in inSlot.meta.axistags]
            if inSlot.upstream_slot is not None:
                self.Output.meta.assignFrom(inSlot.meta)

                outTagKeys = [ax.key for ax in self.outputs["Output"].meta.axistags]

                if not flag in outTagKeys:
                    if self.AxisIndex.ready():
                        axisindex = self.AxisIndex.value
                    else:
                        axisindex = len(outTagKeys)
                    self.outputs["Output"].meta.axistags.insert(axisindex, vigra.defaultAxistags(flag)[0])

                old_c = c
                if flag in inTagKeys:
                    c += inSlot.meta.shape[inSlot.meta.axistags.index(flag)]
                else:
                    c += 1
                self.intervals.append((old_c, c))

        if len(self.inputs["Images"]) > 0:
            newshape = list(self.inputs["Images"][0].meta.shape)
            if flag in inTagKeys:
                # here we assume that all axis are present
                axisindex = self.Output.meta.axistags.index(flag)
                newshape[axisindex] = c
            else:
                # FIXME axisindex is not necessarily defined yet (try setValue on subslot)
                newshape.insert(axisindex, c)
                ideal_blockshape = self.Output.meta.ideal_blockshape
                if ideal_blockshape is not None:
                    ideal_blockshape = ideal_blockshape[:axisindex] + (1,) + ideal_blockshape[axisindex:]
                    self.Output.meta.ideal_blockshape = ideal_blockshape

                max_blockshape = self.Output.meta.max_blockshape
                if max_blockshape is not None:
                    max_blockshape = max_blockshape[:axisindex] + (1,) + max_blockshape[axisindex:]
                    self.Output.meta.max_blockshape = max_blockshape

            self.outputs["Output"].meta.shape = tuple(newshape)
        else:
            self.outputs["Output"].meta.shape = None

    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start, rroi.stop)

        cnt = 0
        written = 0
        start, stop = roi.sliceToRoi(key, self.outputs["Output"].meta.shape)
        assert (stop <= self.outputs["Output"].meta.shape).all()
        # axisindex = self.inputs["AxisIndex"].value
        flag = self.inputs["AxisFlag"].value
        axisindex = self.outputs["Output"].meta.axistags.index(flag)
        # ugly-ugly-ugly
        oldkey = list(key)
        oldkey.pop(axisindex)

        # print "STACKER: ", flag, axisindex
        # print "requesting an outslot from stacker:", key, result.shape
        # print "input slots total: ", len(self.inputs['Images'])
        requests = []

        pool = RequestPool()

        for i, inSlot in enumerate(self.inputs["Images"]):
            req = None
            inTagKeys = [ax.key for ax in inSlot.meta.axistags]
            if flag in inTagKeys:
                slices = inSlot.meta.shape[axisindex]
                if (
                    cnt + slices >= start[axisindex]
                    and start[axisindex] - cnt < slices
                    and start[axisindex] + written < stop[axisindex]
                ):
                    begin = 0
                    if cnt < start[axisindex]:
                        begin = start[axisindex] - cnt
                    end = slices
                    if cnt + end > stop[axisindex]:
                        end -= cnt + end - stop[axisindex]
                    key_ = copy.copy(oldkey)
                    key_.insert(axisindex, slice(begin, end, None))
                    reskey = [slice(None, None, None) for x in range(len(result.shape))]
                    reskey[axisindex] = slice(written, written + end - begin, None)

                    req = inSlot[tuple(key_)].writeInto(result[tuple(reskey)])
                    written += end - begin
                cnt += slices
            else:
                if cnt >= start[axisindex] and start[axisindex] + written < stop[axisindex]:
                    # print "key: ", key, "reskey: ", reskey, "oldkey: ", oldkey
                    # print "result: ", result.shape, "inslot:", inSlot.meta.shape
                    reskey = [slice(None, None, None) for s in oldkey]
                    reskey.insert(axisindex, written)
                    destArea = result[tuple(reskey)]
                    req = inSlot[tuple(oldkey)].writeInto(destArea)
                    written += 1
                cnt += 1

            if req is not None:
                pool.add(req)

        pool.wait()
        pool.clean()

    def propagateDirty(self, inputSlot, subindex, roi):
        roi = copy.copy(roi)
        if not self.Output.ready():
            # If we aren't even fully configured, there's no need to notify downstream slots about dirtiness
            return
        if inputSlot in (self.AxisFlag, self.AxisIndex, self.Images):
            # Any upstream change will cause the whole output to be set dirty
            # Often enough this would happen eventually (e.g. stacking the output
            # of different Filter operators, all connected to the same input).
            self.propagateDirtyIfNewModTime()

        else:
            assert False, "Unknown input slot."


class OpSingleChannelSelector(Operator):
    name = "SingleChannelSelector"
    description = "Select One channel from a Multichannel Image"

    Input = InputSlot()
    Index = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        channelAxis = self.Input.meta.axistags.channelIndex
        inshape = list(self.Input.meta.shape)
        outshape = list(inshape)
        outshape.pop(channelAxis)
        outshape.insert(channelAxis, 1)
        outshape = tuple(outshape)

        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.shape = outshape

        ideal = self.Output.meta.ideal_blockshape
        if ideal is not None and len(ideal) == len(inshape):
            ideal = numpy.asarray(ideal, dtype=numpy.int64)
            ideal[channelAxis] = 1
            self.Output.meta.ideal_blockshape = tuple(ideal)

        max_blockshape = self.Output.meta.max_blockshape
        if max_blockshape is not None and len(max_blockshape) == len(inshape):
            max_blockshape = numpy.asarray(max_blockshape, dtype=numpy.int64)
            max_blockshape[channelAxis] = 1
            self.Output.meta.max_blockshape = tuple(max_blockshape)

        # Output can't be accessed unless the input has enough channels
        # We can't assert here because it's okay to configure this slot incorrectly as long as it is never accessed.
        # Because the order of callbacks isn't well defined, people may not disconnect this operator from its
        #  upstream_slot until after it has already been configured.
        # Again, that's okay as long as it isn't accessed.
        # assert self.Input.meta.getTaggedShape()['c'] > self.Index.value, \
        #        "Requested channel {} is out of range of input data (shape {})".format(self.Index.value, self.Input.meta.shape)
        if self.Input.meta.getTaggedShape()["c"] <= self.Index.value:
            self.Output.meta.NOTREADY = True

    def execute(self, slot, subindex, roi, result):
        index = self.inputs["Index"].value
        channelIndex = self.Input.meta.axistags.channelIndex
        assert (
            self.inputs["Input"].meta.shape[channelIndex] > index
        ), "Requested channel, {}, is out of Range (input shape is {})".format(index, self.Input.meta.shape)

        # Only ask for the channel we need
        key = roiToSlice(roi.start, roi.stop)
        newKey = list(key)
        newKey[channelIndex] = slice(index, index + 1, None)
        # newKey = key[:-1] + (slice(index,index+1),)
        self.inputs["Input"][tuple(newKey)].writeInto(result).wait()
        return result

    def propagateDirty(self, slot, subindex, roi):
        key = roi.toSlice()
        if slot == self.Input:
            channelIndex = self.Input.meta.axistags.channelIndex
            newKey = list(key)
            newKey[channelIndex] = slice(0, 1, None)
            # key = key[:-1] + (slice(0,1,None),)
            self.outputs["Output"].setDirty(tuple(newKey))
        else:
            self.Output.setDirty(slice(None))


class OpSubRegion(Operator):
    """
    Select a subregion from a larger input.
    For example, could be used to select a (10,10) region from an input of (100,100),
        in which case the Input has shape (100,100) and the Output has shape (10,10)

    This operator has been rewritten and has the following differences compared old implementation of OpSubRegion:
    - Takes a single Roi input instead of separate Start/Stop inputs.
    - Since start/stop are provided in one slot, they are applied at the same time and can never be out-of-sync.
    - Always propagates dirty state.
    - Simpler implementation...
    """

    Input = InputSlot(allow_mask=True)
    Roi = InputSlot()  # value slot. value is a tuple: (start, stop)
    Output = OutputSlot(allow_mask=True)

    def setupOutputs(self):
        self._roi = self.Roi.value
        assert isinstance(self._roi[0], tuple)
        assert isinstance(self._roi[1], tuple)
        start, stop = list(map(TinyVector, self._roi))
        if not (len(start) == len(stop) == len(self.Input.meta.shape)):
            # Roi dimensionality must match shape dimensionality
            self.Output.meta.NOTREADY = True
        elif (start >= stop).any():
            # start/stop not compatible (output shape would be negative...)
            self.Output.meta.NOTREADY = True
        else:
            self.Output.meta.assignFrom(self.Input.meta)
            self.Output.meta.shape = tuple(stop - start)

    def execute(self, slot, subindex, output_roi, result):
        input_roi = numpy.array((output_roi.start, output_roi.stop))
        input_roi += self._roi[0]
        input_roi = list(map(tuple, input_roi))
        self.Input(*input_roi).writeInto(result).wait()
        return result

    def propagateDirty(self, dirtySlot, subindex, input_dirty_roi):
        input_dirty_roi = (input_dirty_roi.start, input_dirty_roi.stop)
        if len(input_dirty_roi[0]) != len(self._roi[0]):
            # The dimensionality of the data is changing.
            # The whole workflow must be updating, so don't bother with dirty notifications.
            return
        intersection = getIntersection(input_dirty_roi, self._roi, False)
        if intersection:
            output_dirty_roi = numpy.array(intersection)
            output_dirty_roi -= self._roi[0]
            output_dirty_roi = list(map(tuple, output_dirty_roi))
            self.Output.setDirty(*output_dirty_roi)


class OpMultiArrayMerger(Operator):
    Inputs = InputSlot(level=1)
    MergingFunction = InputSlot()
    Output = OutputSlot()

    name = "Merge Multi Arrays based on a variadic merging function"
    category = "Misc"

    def setupOutputs(self):
        first_meta = self.inputs["Inputs"][0].meta

        self.outputs["Output"].meta.assignFrom(first_meta)
        self.outputs["Output"].meta.dtype = self.inputs["Inputs"][0].meta.dtype

        for input in self.inputs["Inputs"]:
            assert input.meta.shape == first_meta.shape, "Only possible merging consistent shapes"
            assert input.meta.axistags == first_meta.axistags, "Only possible merging same axistags"

        # If *all* inputs have a drange, then provide a drange for the output.
        # Note: This assumes the merging function is pixel-wise
        dranges = []
        for i, slot in enumerate(self.Inputs):
            dr = slot.meta.drange
            if dr is not None:
                dranges.append(numpy.array(dr))
            else:
                dranges = []
                break

        if len(dranges) > 0:
            fun = self.MergingFunction.value
            outRange = fun(dranges)
            self.Output.meta.drange = tuple(outRange)

    def execute(self, slot, subindex, roi, result):
        key = roiToSlice(roi.start, roi.stop)
        requests = []
        for input in self.inputs["Inputs"]:
            requests.append(input[key])

        data = []
        for req in requests:
            data.append(req.wait())

        fun = self.inputs["MergingFunction"].value

        result[:] = fun(data)
        return result

    def propagateDirty(self, dirtySlot, subindex, roi):
        if dirtySlot == self.MergingFunction:
            self.Output.setDirty(slice(None))
        elif dirtySlot == self.Inputs:
            # Assumes a pixel-wise merge function.
            key = roi.toSlice()
            self.Output.setDirty(key)


class OpMaxChannelIndicatorOperator(Operator):
    """
    Produces a bool image where each value is either 0 or 1, depending on whether
    or not that channel of the input is the max value at that pixel compared
    to the other channels.

    Note: it is expected that Output Rois are always single channel.
    """

    Input = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        assert self.Input.meta.getAxisKeys()[-1] == "c", "This operator assumes that the last axis is the channel axis."
        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.dtype = numpy.uint8
        self.Output.meta.drange = (0, 1)

    def execute(self, slot, subindex, roi, result):
        *key, c = roi.toSlice()

        n_channels_requested = c.stop - c.start
        if n_channels_requested != 1:
            raise InvalidRoiException(f"This operator only accepts slices of size 1 for c! Got {n_channels_requested}.")

        data = self.Input[(*key, slice(None))].wait()

        # special case, when data is all zeros (e.g. directly from frozen cache w/o trained classifier)
        if not numpy.any(data):
            result[:] = 0
            return

        result[:] = numpy.uint8(numpy.argmax(data, axis=-1) == c.start)[..., numpy.newaxis]

    def propagateDirty(self, slot, subindex, roi):
        key = roi.toSlice()
        if slot == self.Input:
            self.outputs["Output"].setDirty(key)


class OpPixelOperator(Operator):
    name = "OpPixelOperator"
    description = "simple pixel operations"

    Input = InputSlot()
    Function = InputSlot()
    Output = OutputSlot()

    def __init__(self, graph=None, parent=None, Input=None, Function=None):
        super().__init__(graph=graph, parent=parent)
        self.Input.setOrConnectIfAvailable(Input)
        self.Function.setOrConnectIfAvailable(Function)

    def setupOutputs(self):
        self.function = self.inputs["Function"].value

        self.Output.meta.assignFrom(self.Input.meta)

        # To determine the output dtype, we'll test the function on a tiny array.
        # For pathological functions, this might raise an exception (e.g. divide by zero).
        testInputData = numpy.array([1], dtype=self.Input.meta.dtype)
        self.Output.meta.dtype = self.function(testInputData).dtype.type

        # Provide a default drange.
        # Works for monotonic functions.
        drange_in = self.Input.meta.drange
        if drange_in is not None:
            drange_out = self.function(numpy.array(drange_in))
            self.Output.meta.drange = tuple(drange_out)

    def execute(self, slot, subindex, roi, result):
        key = roiToSlice(roi.start, roi.stop)

        req = self.inputs["Input"][key]
        # Re-use the result array as a temporary variable (if possible)
        if self.Input.meta.dtype == self.Output.meta.dtype:
            req.writeInto(result)
        matrix = req.wait()
        result[:] = self.function(matrix)
        return result

    def propagateDirty(self, slot, subindex, roi):
        key = roi.toSlice()
        if slot == self.Input:
            self.outputs["Output"].setDirty(key)
        elif slot == self.Function:
            self.Output.setDirty(slice(None))
        else:
            assert False, "Unknown dirty input slot"

    @property
    def shape(self):
        return self.outputs["Output"].meta.shape

    @property
    def dtype(self):
        return self.outputs["Output"].meta.dtype


class OpMultiInputConcatenater(Operator):
    name = "OpMultiInputConcatenater"
    description = "Combine two or more MultiInput slots into a single MultiOutput slot"

    Inputs = InputSlot(level=2, optional=True)
    Output = OutputSlot(level=1)

    def __init__(self, *args, **kwargs):
        super(OpMultiInputConcatenater, self).__init__(*args, **kwargs)
        self._numInputLists = 0

    def getOutputIndex(self, inputMultiSlot, inputIndex):
        """
        Determine which output index corresponds to the given input multislot and index.
        """
        # Determine the corresponding output index
        outputIndex = 0
        # Search for the input slot
        for index, multislot in enumerate(self.Inputs):
            if inputMultiSlot != multislot:
                # Not the resized slot.  Skip all its subslots
                outputIndex += len(multislot)
            else:
                # Found the resized slot.  Add the offset and stop here.
                outputIndex += inputIndex
                return outputIndex

        assert False

    def handleInputInserted(self, resizedSlot, inputPosition, totalsize):
        """
        A slot was inserted in one of our inputs.
        Insert a slot in the appropriate location of our output, and connect it to the appropriate input subslot.
        """
        # Determine which output slot this corresponds to
        outputIndex = self.getOutputIndex(resizedSlot, inputPosition)

        # Insert new output slot and connect it up.
        newOutputLength = len(self.Output) + 1
        self.Output.insertSlot(outputIndex, newOutputLength)
        self.Output[outputIndex].connect(resizedSlot[inputPosition])

    def handleInputRemoved(self, resizedSlot, inputPosition, totalsize):
        """
        A slot was removed from one of our inputs.
        Remove the appropriate slot from our output.
        """
        # Determine which output slot this corresponds to
        outputIndex = self.getOutputIndex(resizedSlot, inputPosition)

        # Remove the corresponding output slot
        newOutputLength = len(self.Output) - 1
        self.Output.removeSlot(outputIndex, newOutputLength)

    def setupOutputs(self):
        # This function is merely provided to initialize ourselves if one of our input lists was set up in advance.
        # We don't need to do this expensive rebuilding of the output list unless a new input list was added
        if self._numInputLists == len(self.Inputs):
            return

        self._numInputLists = len(self.Inputs)

        # First pass to determine output length
        totalOutputLength = 0
        for index, slot in enumerate(self.Inputs):
            totalOutputLength += len(slot)

        self.Output.resize(totalOutputLength)

        # Second pass to make connections and subscribe to future changes
        outputIndex = 0
        for index, slot in enumerate(self.Inputs):
            slot.notifyInserted(self.handleInputInserted)
            slot.notifyRemove(self.handleInputRemoved)

            # Connect subslots to output
            for i, s in enumerate(slot):
                self.Output[outputIndex].connect(s)
                outputIndex += 1

    def execute(self, slot, subindex, roi, result):
        # Should never be called.  All output slots are directly connected to an input slot.
        assert False

    def propagateDirty(self, inputSlot, subindex, roi):
        # Nothing to do here.
        # All outputs are directly connected to an input slot.
        pass


class OpWrapSlot(Operator):
    """
    Adaptor for when you have a slot and you need to make it look like a multi-slot.
    Converts a single slot into a multi-slot of len == 1 and level == 1
    """

    Input = InputSlot()
    Output = OutputSlot(level=1)

    def __init__(self, *args, **kwargs):
        super(OpWrapSlot, self).__init__(*args, **kwargs)
        self.Output.resize(1)
        self.Output[0].connect(self.Input)

    def setupOutputs(self):
        pass

    def execute(self, slot, subindex, roi, result):
        assert False

    def propagateDirty(self, inputSlot, subindex, roi):
        pass

    def setInSlot(self, slot, subindex, roi, value):
        self.Output[0][roi.toSlice()] = value


class OpDtypeView(Operator):
    """
    Connect an input slot of one dtype to an output with a different
     (but compatible) dtype, WITHOUT creating a copy.
    For example, convert uint32 to int32.

    Note: This operator uses ndarray.view() and must be used with care.
          For example, don't use it to convert a float to an int (or vice-versa),
             and don't use it to convert e.g. uint8 to uint32.
          See ndarray.view() documentation for details.

          For converting between int and float, consider OpPixelOperator,
          which will copy the data.
    """

    Input = InputSlot()
    OutputDtype = InputSlot()

    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.dtype = self.OutputDtype.value
        # self.Output.meta.dtype = numpy.uint32

    def execute(self, slot, subindex, roi, result):
        result_view = result.view(self.Input.meta.dtype)
        self.Input(roi.start, roi.stop).writeInto(result_view).wait()
        return result

    def propagateDirty(self, slot, subindex, roi):
        self.Output.setDirty(roi)


class OpConvertDtype(Operator):
    Input = InputSlot()
    ConversionDtype = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.dtype = self.ConversionDtype.value

    def execute(self, slot, subindex, roi, result):
        if self.Input.meta.dtype == self.ConversionDtype.value:
            self.Input(roi.start, roi.stop).writeInto(result).wait()
        else:
            input_data = self.Input(roi.start, roi.stop).wait()
            result[:] = input_data.astype(self.ConversionDtype.value)

    def propagateDirty(self, slot, subindex, roi):
        if slot is self.ConversionDtype:
            self.Output.setDirty()
        elif slot is self.Input:
            self.Output.setDirty(roi)
        else:
            assert False, "Unknown slot: {}".format(slot.name)


class OpSelectSubslot(Operator):
    """
    Select the Nth subslot from a multi-slot
    """

    SubslotIndex = InputSlot()
    Inputs = InputSlot(level=1, optional=True)
    Output = OutputSlot()

    def setupOutputs(self):
        index = self.SubslotIndex.value
        if len(self.Inputs) > index:
            self.Output.connect(self.Inputs[index])
        else:
            self.Output.disconnect()
            self.Output.meta.NOTREADY = True

    def execute(self, slot, subindex, roi, result):
        pass

    def propagateDirty(self, slot, subindex, roi):
        pass


class OpMultiChannelSelector(Operator):
    """Select a subset of channels from a multichannel image

    Channels are stacked in the same order as provided in SelectedChannels
    input slot.
    """

    Input = InputSlot()
    # tuple mapping output_channels to input channels
    # any mapping is valid, even with repeats (1, 1, 1)
    # channels must exist in input of course
    SelectedChannels = InputSlot(value=(0,))

    Output = OutputSlot()

    def setupOutputs(self):
        if self.Input.meta.getAxisKeys()[-1] != "c":
            raise ValueError("Channel axis must be last for the input")

        channel_axis = -1

        max_channel = self.Input.meta.getTaggedShape()["c"]
        selected_channels: Tuple[int] = self.SelectedChannels.value
        if len(selected_channels) == 0 or any(x >= max_channel for x in selected_channels):
            self.Output.meta.NOTREADY = True
            return

        self.Output.meta.assignFrom(self.Input.meta)

        in_shape = self.Input.meta.shape
        self.Output.meta.shape = (*in_shape[:-1], len(selected_channels))

        ideal = self.Output.meta.ideal_blockshape
        if ideal is not None:
            assert len(ideal) == len(in_shape)
            ideal = numpy.asarray(ideal, dtype=numpy.int64)
            ideal[channel_axis] = 1
            self.Output.meta.ideal_blockshape = tuple(ideal)

        max_blockshape = self.Output.meta.max_blockshape
        if max_blockshape is not None:
            assert len(max_blockshape) == len(in_shape)
            max_blockshape = numpy.asarray(max_blockshape, dtype=numpy.int64)
            max_blockshape[channel_axis] = len(selected_channels)
            self.Output.meta.max_blockshape = tuple(max_blockshape)

    def execute(self, slot, subindex, roi, result):
        channel_indexes: Tuple[int] = self.SelectedChannels.value

        # make sure to request the minimum (consecutive) channels
        input_roi = roi.copy()

        if len(channel_indexes) == 1:
            # Fetch in-place
            channel_index = channel_indexes[0]
            input_roi.start[-1] = channel_index
            input_roi.stop[-1] = channel_index + 1
            self.Input(input_roi.start, input_roi.stop).writeInto(result).wait()

        else:
            pool = RequestPool()

            # get data from the respective channels in the input individually (channel_indexes)
            # and write them to the expected channel in result
            for i, channel in enumerate(channel_indexes):
                # add 1 to channel indices stop for valid single channel slices
                input_roi.start[-1] = channel
                input_roi.stop[-1] = channel + 1
                dest_key = tuple([slice(None) for _ in input_roi.start[:-1]]) + (slice(i, i + 1),)
                req = self.Input(input_roi.start, input_roi.stop).writeInto(result[dest_key])
                pool.add(req)

            pool.wait()
            pool.clean()

    def propagateDirty(self, slot, subindex, roi):
        self.propagateDirtyIfNewModTime()
