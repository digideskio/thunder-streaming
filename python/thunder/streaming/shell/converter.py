from thunder.streaming.shell.analysis import Analysis

from abc import abstractmethod
from collections import OrderedDict
import numpy as np
import re
import os
import json
import struct
import time

# TODO Fix up comment
"""
A converter takes the raw output from an Analysis (a collection of ordered binary files) and combines them into a
usable format (i.e. an image, or a JSON document to send to Lightning).

Every converter must be decorated with the @converter decorator, so that they will be dynamically added to the
Analysis class. Doing this enables the following workflow:

*********************************************************

What's the most desirable output pattern?

analysis = Analysis.ExampleAnalysis(...).toSeries()
                .toImageFile(path)
                .toLightningServer(lgn)

or...

analysis = Analysis.ExampleAnalysis(...).toSeries()
output1 = LightningOutput(lgn)
output2 = ImagesOutput(path)
analysis.addOutputs(output1, output2)

*********************************************************

(output_loc is an interface for receiving the output data from an Analysis. It should be pluggable (so that we can
read the parts from disk, or from the network when that's available)

@converter
def seriesToImage(analysis):
    (parse the files in the Analysis' output directory and convert them into an image)

analysis1 = Analysis.ExampleAnalysis(param1=value1, param2=value2...).seriesToImage()

"""


class Data(object):
    """
    An abstract base class for all objects returned by a converter function. Output objects can define a set of
    functions to send their contents to external locations:

    # converted defines the type-specific output function
    converted = Analysis.ExampleAnalysis(...).toSeries()

    Retains a reference to the Analysis that created it with self.analysis (this is used to start the Analysis
    thread)
    """

    @property
    def identifier(self):
        return self.__class__.__name__

    def __init__(self, analysis):
        self.analysis = analysis
        # Output functions are added to output_funcs with the @output decorator
        self.output_funcs = {}

    @staticmethod
    def output(func):
        def add_to_output(self, *args, **kwargs):
            self.output_funcs[func.func_name] = lambda data: func(self, data, *args, **kwargs)
            return self.analysis
        return add_to_output

    @staticmethod
    def converter(func):
        """
        :param func: A function with a single parameter type, Analysis, that must return an instance of Data
        :return: The function after it's been added to Analysis' dict
        """
        def add_output(analysis, **kwargs):
            output = func(analysis, **kwargs)
            analysis.outputs.append(output)
            return output
        print "Adding %s to Analysis.__dict__" % func.func_name
        setattr(Analysis, func.func_name, add_output)
        return func

    @abstractmethod
    def _convert(self, root, new_data):
        return None

    def handle_new_data(self, root, new_data):
        converted = self._convert(root, new_data)
        for func in self.output_funcs.values():
            func(converted)

    def start(self):
        self.analysis.start()

    def stop(self):
        self.analysis.stop()


# Some example Converters for StreamingSeries

class Series(Data):

    DIMS_FILE_NAME = "dimensions.json"
    RECORD_SIZE = "record_size"
    DTYPE = "dtype"
    DIMS_PATTERN = re.compile(DIMS_FILE_NAME)

    def __init__(self, analysis, dtype="float16"):
        Data.__init__(self, analysis)
        self.dtype = dtype

    @staticmethod
    @Data.converter
    def toSeries(analysis):
        """
        :param analysis: The analysis whose raw output will be parsed and converted into an in-memory series
        :return: A Series object
        """
        return Series(analysis)

    def _get_dims(self, root):
        try:
            dims = open(os.path.join(root, Series.DIMS_FILE_NAME), 'r')
            dims_json = json.load(dims)
            record_size = int(dims_json[self.RECORD_SIZE])
            dtype = dims_json[self.DTYPE]
            return record_size, dtype
        except Exception as e:
            print "Cannot load binary series: %s" % str(e)
            return None, None

    def _loadBinaryFromPath(self, p, dtype):
        fbuf = open(p, 'rb').read()
        return fbuf

    def _saveBinaryToPath(self, p, data):
        print "In _saveBinaryToPath, saving to: %s" % p
        if data is None or len(data) == 0:
            return
        with open(p, 'w+') as f:
            f.write(data)

    def _convert(self, root, new_data):

        num_output_files = len(new_data) - 1

        def get_partition_num(output_name):
            split_name = output_name.split('-')
            if len(split_name) == 3:
                return int(split_name[1])
            return num_output_files

        # Load in the dimension JSON file (which we assume exists in the results directory)
        record_size, dtype = self._get_dims(root)
        if not record_size or not dtype:
            return None
        self.dtype = dtype

        #merged_series = np.array([], dtype=dtype)
        without_dims = filter(lambda x: not self.DIMS_PATTERN.search(x), new_data)
        sorted_files = sorted(without_dims, key=get_partition_num)
        bytes = ''
        for f in sorted_files:
            series = self._loadBinaryFromPath(f, dtype)
            bytes = bytes + series
            #merged_series = np.append(merged_series, series)
        merged_series = np.frombuffer(bytes, dtype=dtype)
        reshaped_series = merged_series.reshape(-1, record_size)
        return reshaped_series

    @Data.output
    def toLightning(self, data, lgn, only_viz=False):
        if data is None or len(data) == 0:
            return
        if only_viz:
            print "Appending %s to existing line viz." % str(data)
            lgn.append(data)
        else:
            # Do dashboard stuff here
            lgn.line(data)

    @Data.output
    def toFile(self, data, path=None, prefix=None, fileType='bin'):
        """
        If prefix is specified, a different file will be written out at every batch. If not, the same image file will be
        overwritten.
        """
        # TODO implement saving with keys as well
        if path:
            fullPath = path if not prefix else path + '-' + str(time.time())
            fullPath = fullPath + '.' + fileType
            self._saveBinaryToPath(fullPath, data)
        else:
            print "Path not specified in toFile."


class Image(Series):
    """
    Represents a 2 or 3 dimensional image
    """

    def __init__(self, analysis, dims, clip):
        Series.__init__(self, analysis)
        self.dims = dims
        self.clip = clip

    @staticmethod
    @Data.converter
    def toImage(analysis, dims=(512, 512, 4), clip=400):
        """
        :param analysis: The analysis whose raw output will be parsed and converted into an in-memory image
        :return: An Image object
        """
        return Image(analysis, dims, clip)

    def _convert(self, root, new_data):
        series = Series._convert(self, root, new_data)
        if series is not None and len(series) != 0:
            # Remove the regressors
            series = series[:-3]
            # Sort the keys/values
            image_arr = series.clip(0, self.clip).reshape(self.dims)
            return image_arr

    def _getPlaneData(self, data, plane):
        plane_size = self.dims[0] * self.dims[1]
        return data[plane * plane_size:(plane + 1) * plane_size]

    @Data.output
    def toLightning(self, data, lgn, plane=0, only_viz=False):
        if data is None or len(data) == 0:
            return
        if len(self.dims) > 3 or len(self.dims) < 1:
            print "Invalid images dimensions (must be < 3 and >= 1)"
            return
        plane_data = self._getPlaneData(data, plane)
        if only_viz:
            lgn.update(plane_data)
        else:
            # Do dashboard stuff here
            lgn.image(plane_data)
