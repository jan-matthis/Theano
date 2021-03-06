import os
import numpy
import warnings

import theano
from theano import Op, Apply, tensor, config, Variable
from theano.scalar import as_scalar, constant, Log
from theano.gradient import DisconnectedType, grad_not_implemented
from theano.gof import Optimizer, local_optimizer, COp
from theano.gof.cmodule import GCC_compiler
from theano.gof.type import CDataType, Generic
from theano.compile import optdb
from theano.compile.ops import shape_i
from theano.tensor.nnet import SoftmaxGrad
from theano.tensor.signal.downsample import (
    DownsampleFactorMax, MaxPoolGrad, AveragePoolGrad)

from . import pygpu, init_dev
from .basic_ops import (as_gpuarray_variable,
                        gpu_contiguous, HostFromGpu,
                        GpuAllocEmpty, empty_like)
from .elemwise import GpuElemwise
from .conv import GpuConv

# These don't exist in gpuarray
# GpuDownsampleFactorMax, GpuDownsampleFactorMaxGrad
from .nnet import GpuSoftmax
from .opt import gpu_seqopt, register_opt, conv_groupopt, op_lifter
from .opt_util import alpha_merge, output_merge

# We need to import this to define the flags.
from theano.sandbox import dnn_flags  # noqa


def dnn_available():
    if dnn_available.avail is not None:
        return dnn_available.avail
    if pygpu is None:
        dnn_available.msg = "PyGPU not available"
        dnn_available.avail = False
        return False
    if not init_dev.device.startswith('cuda'):
        dnn_available.msg = "Not on a CUDA device. Got %s." % init_dev.device
        dnn_available.avail = False
        return False
    # This is a hack because bin_id is in the from of
    # "sm_<major><minor>" for cuda devices.
    if pygpu.get_default_context().bin_id[:-2] < '30':
        dnn_available.msg = "Device not supported by cuDNN"
        dnn_available.avail = False
    preambule = """
#include <stdio.h>
#include <cuda.h>
#include <cudnn.h>
#include <cudnn_helper.h>
"""

    body = """
cudnnHandle_t _handle = NULL;
cudnnStatus_t err;
if ((err = cudnnCreate(&_handle)) != CUDNN_STATUS_SUCCESS) {
  fprintf(stderr, "could not create cuDNN handle: %s",
          cudnnGetErrorString(err));
  return 1;
}
"""
    # Do not run here the test program. It would run on the
    # default gpu, not the one selected by the user. If mixed
    # GPU are installed or if the GPUs are configured in
    # exclusive mode, this cause bad detection.
    comp, out, err = GCC_compiler.try_flags(
        ["-l", "cudnn", "-I" + os.path.dirname(__file__),
         "-I" + config.dnn.include_path,
         "-L" + config.dnn.library_path],
        preambule=preambule, body=body,
        try_run=False, output=True)

    dnn_available.avail = comp
    if not dnn_available.avail:
        dnn_available.msg = (
            "Theano cannot compile with cuDNN. We got this error:\n" +
            str(err))
    else:
        # If we can compile, check that we can import and run.
        v = version()
        if v < 2000:
            dnn_available.avail = False
            dnn_available.msg = (
                "You have an old release of CuDNN (or a release candidate) "
                "that isn't supported.  Please update to at least v2 final "
                "version.")
            raise RuntimeError(dnn_available.msg)
        if v >= 3000 and v < 3007:
            dnn_available.avail = False
            dnn_available.msg = (
                "You have installed a release candidate of CuDNN v3. This "
                "isn't supported. Please update to v3 final version.")
            raise RuntimeError(dnn_available.msg)

    return dnn_available.avail

dnn_available.avail = None
dnn_available.msg = None


class DnnBase(COp):
    """
    Creates a handle for cudnn and pulls in the cudnn libraries and headers.

    """
    # dnn does not know about broadcasting, so we do not need to assert
    # the input broadcasting pattern.
    check_broadcast = False

    def __init__(self, files=None, c_func=None):
        if files is None:
            files = []
        COp.__init__(self, ["dnn_base.c"] + files, c_func)

    def c_headers(self):
        return ['cudnn.h', 'cudnn_helper.h', 'gpuarray_helper.h',
                'gpuarray/types.h', 'gpuarray/array.h', 'gpuarray/util.h',
                'gpuarray/ext_cuda.h', 'gpuarray_api.h', 'numpy_compat.h']

    def c_header_dirs(self):
        return [os.path.dirname(__file__), pygpu.get_include(),
                config.dnn.include_path]

    def c_libraries(self):
        return ['cudnn', 'gpuarray']

    def c_lib_dirs(self):
        return [config.dnn.library_path]

    def c_code_cache_version(self):
        return (super(DnnBase, self).c_code_cache_version(), version())


class DnnVersion(Op):
    __props__ = ()

    def c_headers(self):
        return ['cudnn.h']

    def c_header_dirs(self):
        return [config.dnn.include_path]

    def c_libraries(self):
        return ['cudnn']

    def c_lib_dirs(self):
        return [config.dnn.library_path]

    def c_support_code(self):
        return """
#if PY_MAJOR_VERSION >= 3
#define PyInt_FromLong PyLong_FromLong
#endif
"""

    def make_node(self):
        return Apply(self, [], [Generic()()])

    def c_code(self, node, name, inputs, outputs, sub):
        o = outputs[0]
        return """
        %(o)s = PyTuple_Pack(2, PyInt_FromLong(CUDNN_VERSION), PyInt_FromLong(cudnnGetVersion()));
        """ % locals()

    def do_constant_folding(self, node):
        # Needed as we do not want to cache this information.
        return False

    def c_code_cache_version(self):
        # Not needed, but make it clear that we do not want to cache this.
        return None


def version():
    """
    Return the current cuDNN version we link with.

    This also does a check that the header version matches the runtime version.
    """
    if not dnn_available():
        raise Exception(
            "We can't determine the cudnn version as it is not available",
            dnn_available.msg)

    if version.v is None:
        f = theano.function([], DnnVersion()(),
                            theano.Mode(optimizer=None),
                            profile=False)
        v = f()
        if v[0] != v[1]:
            raise RuntimeError("Mixed dnn version. The header is version %s "
                               "while the library is version %s." % v)
        version.v = v[1]
    return version.v
version.v = None


class GpuDnnConvDesc(COp):
    """
    This Op builds a convolution descriptor for use in the other convolution
    operations.

    See the doc of :func:`dnn_conv` for a description of the parameters

    """

    __props__ = ('border_mode', 'subsample', 'conv_mode')

    def c_headers(self):
        return ['cudnn.h', 'cudnn_helper.h']

    def c_header_dirs(self):
        return [os.path.dirname(__file__), config.dnn.include_path]

    def c_libraries(self):
        return ['cudnn']

    def c_lib_dirs(self):
        return [config.dnn.library_path]

    def do_constant_folding(self, node):
        return False

    def __init__(self, border_mode, subsample=(1, 1), conv_mode='conv'):
        COp.__init__(self, ["conv_desc.c"], "APPLY_SPECIFIC(conv_desc)")

        if isinstance(border_mode, int):
            border_mode = (border_mode,) * len(subsample)
        if isinstance(border_mode, tuple):
            assert len(border_mode) == len(subsample)
            border_mode = tuple(map(int, border_mode))
        if not ((isinstance(border_mode, tuple) and min(border_mode) >= 0) or
                border_mode in ('valid', 'full')):
            raise ValueError(
                'invalid border_mode {}, which must be either '
                '"valid", "full", an integer or a pair of'
                ' integers'.format(border_mode))
        self.border_mode = border_mode
        assert len(subsample) in (2, 3)
        self.subsample = subsample
        assert conv_mode in ('conv', 'cross')
        self.conv_mode = conv_mode

    def make_node(self, kern_shape):
        if kern_shape.type.ndim != 1 or kern_shape.type.dtype != 'int64':
            raise TypeError('kern must be 1D shape tensor')

        return Apply(self, [kern_shape],
                     [CDataType("cudnnConvolutionDescriptor_t",
                                freefunc="cudnnDestroyConvolutionDescriptor")()])

    def get_op_params(self):
        pad0 = '0'
        pad1 = '0'
        pad2 = '0'
        if isinstance(self.border_mode, tuple):
            pad0 = str(self.border_mode[0])
            pad1 = str(self.border_mode[1])
            if len(self.border_mode) > 2:
                pad2 = str(self.border_mode[2])
            bmode = '2'
        elif self.border_mode == "valid":
            bmode = '1'
        elif self.border_mode == "full":
            bmode = '0'
        else:
            raise ValueError("Invalid value for border_mode")

        if self.conv_mode == 'conv':
            conv_flag = 'CUDNN_CONVOLUTION'
        else:
            conv_flag = 'CUDNN_CROSS_CORRELATION'

        sub0 = str(self.subsample[0])
        sub1 = str(self.subsample[1])
        if len(self.subsample) > 2:
            sub2 = str(self.subsample[2])
        else:
            sub2 = '0'

        return [('NB_DIMS', str(len(self.subsample))),
                ('BORDER_MODE', bmode),
                ('PAD_0', pad0), ('PAD_1', pad1), ('PAD_2', pad2),
                ('CONV_MODE', conv_flag),
                ('SUB_0', sub0), ('SUB_1', sub1), ('SUB_2', sub2)]

    def c_code_cache_version(self):
        return (super(GpuDnnConvDesc, self).c_code_cache_version(), version())

# scalar constants
_zero = constant(numpy.asarray(0.0, dtype='float64'))
_one = constant(numpy.asarray(1.0, dtype='float64'))


def ensure_dt(val, default, name, dtype):
    if val is None:
        val = default.clone()
    if not isinstance(val, Variable):
        val = constant(val)
    if hasattr(val, 'ndim') and val.ndim == 0:
        val = as_scalar(val)
    if not isinstance(val.type, theano.scalar.Scalar):
        raise TypeError("%s: expected a scalar value" % (name,))
    if not val.type.dtype == dtype:
        val = val.astype(dtype)
    return val


class GpuDnnConv(DnnBase):
    """
    The forward convolution.

    Parameters
    ----------
    image
    kernel
    descr
        The convolution descriptor.
    algo : {'small', 'none', 'large', 'fft', 'guess_once', 'guess_on_shape_change', 'time_once', 'time_on_shape_change'}
        Default is the value of :attr:`config.dnn.conv.algo_fwd`.

    """

    __props__ = ('algo', 'inplace')

    def __init__(self, algo=None, inplace=False):
        DnnBase.__init__(self, ["dnn_conv_base.c", "dnn_fwd.c"],
                         "APPLY_SPECIFIC(conv_fwd)")

        if algo is None:
            algo = config.dnn.conv.algo_fwd
        self.algo = algo

        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [2]}

        if version() < 3000:
            if self.algo == 'fft':
                raise RuntimeError("CuDNN FFT convolution requires CuDNN v3")
            elif self.algo in ['guess_once', 'guess_on_shape_change']:
                raise RuntimeError("CuDNN selection of convolution "
                                   "implementation based on heuristics "
                                   "requires CuDNN v3")
            elif self.algo in ['time_once', 'time_on_shape_change']:
                raise RuntimeError("CuDNN convolution timing requires CuDNN v3")

        assert self.algo in ['none', 'small', 'large', 'fft', 'guess_once',
                             'guess_on_shape_change', 'time_once',
                             'time_on_shape_change']

    def __setstate__(self, d):
        self.__dict__.update(d)
        if not hasattr(self, 'algo'):
            if hasattr(self, 'workmem'):
                self.algo = self.workmem
            else:
                self.algo = config.dnn.conv.algo_fwd
        if not hasattr(self, 'inplace'):
            self.inplace = False

    def get_op_params(self):
        defs = []
        if self.inplace:
            defs.append(('CONV_INPLACE', '1'))

        alg = 'CUDNN_CONVOLUTION_FWD_ALGO_IMPLICIT_PRECOMP_GEMM'
        if self.algo == 'none':
            alg = 'CUDNN_CONVOLUTION_FWD_ALGO_IMPLICIT_GEMM'
        elif self.algo == 'small':
            alg = 'CUDNN_CONVOLUTION_FWD_ALGO_IMPLICIT_PRECOMP_GEMM'
        elif self.algo == 'large':
            alg = 'CUDNN_CONVOLUTION_FWD_ALGO_GEMM'
        elif self.algo == 'fft':
            alg = 'CUDNN_CONVOLUTION_FWD_ALGO_FFT'
        defs.append(('CONV_ALGO', alg))

        if self.algo in ['guess_once', 'guess_on_shape_change',
                         'time_once', 'time_on_shape_change']:
            defs.append(('CHOOSE_ALGO', ''))
        if self.algo in ['guess_once', 'time_once']:
            defs.append(('CHOOSE_ONCE', ''))
        if self.algo in ['time_once', 'time_on_shape_change']:
            defs.append(('CHOOSE_TIME', ''))

        return defs

    def make_node(self, img, kern, output, desc, alpha=None, beta=None):
        img = as_gpuarray_variable(img)
        kern = as_gpuarray_variable(kern)
        output = as_gpuarray_variable(output)
        if img.type.ndim not in (4, 5):
            raise TypeError('img must be 4D or 5D tensor')
        if kern.type.ndim not in (4, 5):
            raise TypeError('kern must be 4D or 5D tensor')
        if output.type.ndim not in (4, 5):
            raise TypeError('output must be a 4D or 5D tensor')

        if (img.type.ndim != kern.type.ndim or
                img.type.ndim != output.type.ndim):
            raise TypeError("The number of dimensions of "
                            "img, kern and output must match")

        if img.type.ndim == 5 and self.algo == 'fft':
            raise ValueError("convolution algo fft can't be used for "
                             "3d convolutions")

        if (not isinstance(desc.type, CDataType) or
                desc.type.ctype != 'cudnnConvolutionDescriptor_t'):
            raise TypeError('desc must be cudnnConvolutionDescriptor_t')

        alpha = ensure_dt(alpha, _one, 'alpha', img.dtype)
        beta = ensure_dt(beta, _zero, 'beta', img.dtype)

        return Apply(self, [img, kern, output, desc, alpha, beta],
                     [output.type()])

    def grad(self, inp, grads):
        img, kerns, output, desc, alpha, beta = inp
        top, = grads

        top = gpu_contiguous(top)

        d_img = GpuDnnConvGradI()(kerns, top, empty_like(img), desc)
        d_kerns = GpuDnnConvGradW()(img, top, empty_like(kerns), desc)
        d_alpha = grad_not_implemented(self, 4, alpha)
        d_beta = grad_not_implemented(self, 5, beta)

        return [d_img * alpha, d_kerns * alpha, top * beta,
                DisconnectedType()(), d_alpha, d_beta]

    def connection_pattern(self, node):
        # not connected to desc
        return [[1], [1], [1], [0], [1], [1]]

    @staticmethod
    def get_out_shape(ishape, kshape, border_mode, subsample):
        """
        This function computes the output shape for a convolution with
        the specified parameters. `ishape` and `kshape` can be symbolic
        or scalar.

        """
        b = ishape[0]  # Number of inputs
        h = ishape[2]  # Height of input feature maps
        w = ishape[3]  # Width of input feature maps
        nb = kshape[0]  # Number of output feature maps
        kh = kshape[2]  # Height of each filter
        kw = kshape[3]  # Width of each filter

        nd = len(subsample)

        if nd > 2:
            d = ishape[4]
            kd = ishape[4]

        sh = subsample[0]
        sw = subsample[1]
        if nd > 2:
            sd = subsample[2]

        if border_mode == 'full':
            padh = kh - 1
            padw = kw - 1
            if nd > 4:
                padd = kd - 1
        elif isinstance(border_mode, tuple):
            padh = border_mode[0]
            padw = border_mode[1]
            if nd > 2:
                padd = border_mode[2]
        else:
            assert border_mode == 'valid'
            padh = 0
            padw = 0
            padd = 0

        res = [b, nb,
               (h + 2 * padh - kh) // sh + 1,
               (w + 2 * padw - kw) // sw + 1]

        if nd > 2:
            res.append(d + 2 * padd - kd // sd + 1)

        return res

    def infer_shape(self, node, shape):
        return [shape[2]]


class GpuDnnConvGradW(DnnBase):
    """
    The convolution gradient with respect to the weights.

    Parameters
    ----------
    image
    kernel
    descr
        The convolution descriptor.

    """

    __props__ = ('algo', 'inplace')

    def __init__(self, inplace=False, algo=None):
        DnnBase.__init__(self, ["dnn_conv_base.c", "dnn_gw.c"],
                         "APPLY_SPECIFIC(conv_gw)")
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [2]}
        if algo is None:
            algo = config.dnn.conv.algo_bwd
        self.algo = algo
        assert self.algo in ['none', 'deterministic', 'fft', 'guess_once',
                             'guess_on_shape_change', 'time_once',
                             'time_on_shape_change']

    def __setstate__(self, d):
        self.__dict__.update(d)
        if not hasattr(self, 'inplace'):
            self.inplace = False
        if not hasattr(self, 'algo'):
            self.algo = config.dnn.conv.algo_bwd

    def grad(self, inp, grads):
        img, top, output, desc, alpha, beta = inp
        kerns, = grads

        kerns = gpu_contiguous(kerns)

        d_img = GpuDnnConvGradI()(kerns, top, empty_like(img), desc)
        d_top = GpuDnnConv()(img, kerns, empty_like(top), desc)
        d_alpha = grad_not_implemented(self, 4, alpha)
        d_beta = grad_not_implemented(self, 5, beta)

        return (d_img * alpha, d_top * alpha, kerns * beta,
                DisconnectedType()(), d_alpha, d_beta)

    def connection_pattern(self, node):
        # not connected to desc
        return [[1], [1], [1], [0], [1], [1]]

    def get_op_params(self):
        defs = []
        if self.inplace:
            defs.append(('CONV_INPLACE', '1'))

        if version() < 3000:
            alg = '0'
        else:
            alg = 'CUDNN_CONVOLUTION_BWD_FILTER_ALGO_0'
            if self.algo == 'none':
                alg = 'CUDNN_CONVOLUTION_BWD_FILTER_ALGO_0'
            if self.algo == 'deterministic':
                alg = 'CUDNN_CONVOLUTION_BWD_FILTER_ALGO_1'
            if self.algo == 'fft':
                alg = 'CUDNN_CONVOLUTION_BWD_FILTER_ALGO_FFT'

            if self.algo in ['guess_once', 'guess_on_shape_change',
                             'time_once', 'time_on_shape_change']:
                defs.append(('CHOOSE_ALGO', ''))
            if self.algo in ['guess_once', 'time_once']:
                defs.append(('CHOOSE_ONCE', ''))
            if self.algo in ['time_once', 'time_on_shape_change']:
                defs.append(('CHOOSE_TIME', ''))

        defs.append(('CONV_ALGO', alg))

        return defs

    def make_node(self, img, topgrad, output, desc, alpha=None, beta=None):
        img = as_gpuarray_variable(img)
        topgrad = as_gpuarray_variable(topgrad)
        output = as_gpuarray_variable(output)
        if img.type.ndim not in (4, 5):
            raise TypeError('img must be 4D or 5D tensor')
        if topgrad.type.ndim not in (4, 5):
            raise TypeError('topgrad must be 4D or 5D tensor')
        if output.type.ndim not in (4, 5):
            raise TypeError('output must be 4D or 5D tensor')

        if (img.type.ndim != topgrad.type.ndim or
                img.type.ndim != output.type.ndim):
            raise TypeError("The number of dimensions of "
                            "img, topgrad and output must match")

        if img.type.ndim == 5 and self.algo in ['fft', 'deterministic']:
            raise ValueError("convolution algo %s can't be used for "
                             "3d convolutions", (self.algo,))

        if (not isinstance(desc.type, CDataType) or
                desc.type.ctype != 'cudnnConvolutionDescriptor_t'):
            raise TypeError('desc must be cudnnConvolutionDescriptor_t')

        alpha = ensure_dt(alpha, _one, 'alpha', img.dtype)
        beta = ensure_dt(beta, _zero, 'beta', img.dtype)

        return Apply(self, [img, topgrad, output, desc, alpha, beta],
                     [output.type()])

    def infer_shape(self, node, shape):
        return [shape[2]]


class GpuDnnConvGradI(DnnBase):
    """
    The convolution gradient with respect to the inputs.

    Parameters
    ----------
    image
    kernel
    descr
        The convolution descriptor.

    """

    __props__ = ('algo', 'inplace',)

    def __init__(self, inplace=False, algo=None):
        DnnBase.__init__(self, ["dnn_conv_base.c", "dnn_gi.c"],
                         "APPLY_SPECIFIC(conv_gi)")
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [2]}
        if algo is None:
            algo = config.dnn.conv.algo_bwd
        self.algo = algo
        assert self.algo in ['none', 'deterministic', 'fft', 'guess_once',
                             'guess_on_shape_change', 'time_once',
                             'time_on_shape_change']

    def __setstate__(self, d):
        self.__dict__.update(d)
        if not hasattr(self, 'algo'):
            self.algo = config.dnn.conv.algo_bwd
        if not hasattr(self, 'inplace'):
            self.inplace = False

    def grad(self, inp, grads):
        kerns, top, output, desc, alpha, beta = inp
        img, = grads

        img = gpu_contiguous(img)

        d_kerns = GpuDnnConvGradW()(img, top, empty_like(kerns), desc)
        d_top = GpuDnnConv()(img, kerns, empty_like(top), desc)
        d_alpha = grad_not_implemented(self, 4, alpha)
        d_beta = grad_not_implemented(self, 5, beta)

        return (d_kerns * alpha, d_top * alpha, img * beta,
                DisconnectedType()(), d_alpha, d_beta)

    def connection_pattern(self, node):
        # not connected to desc
        return [[1], [1], [1], [0], [1], [1]]

    def get_op_params(self):
        defs = []
        if self.inplace:
            defs.append(('CONV_INPLACE', '1'))

        if version() < 3000:
            alg = '0'
        else:
            alg = 'CUDNN_CONVOLUTION_BWD_DATA_ALGO_0'
            if self.algo == 'none':
                alg = 'CUDNN_CONVOLUTION_BWD_DATA_ALGO_0'
            if self.algo == 'deterministic':
                alg = 'CUDNN_CONVOLUTION_BWD_DATA_ALGO_1'
            if self.algo == 'fft':
                alg = 'CUDNN_CONVOLUTION_BWD_DATA_ALGO_FFT'

            if self.algo in ['guess_once', 'guess_on_shape_change',
                             'time_once', 'time_on_shape_change']:
                defs.append(('CHOOSE_ALGO', ''))
            if self.algo in ['guess_once', 'time_once']:
                defs.append(('CHOOSE_ONCE', ''))
            if self.algo in ['time_once', 'time_on_shape_change']:
                defs.append(('CHOOSE_TIME', ''))

        defs.append(('CONV_ALGO', alg))

        return defs

    def make_node(self, kern, topgrad, output, desc, alpha=None, beta=None):
        kern = as_gpuarray_variable(kern)
        topgrad = as_gpuarray_variable(topgrad)
        output = as_gpuarray_variable(output)
        if kern.type.ndim not in (4, 5):
            raise TypeError('kern must be 4D or 5D tensor')
        if topgrad.type.ndim not in (4, 5):
            raise TypeError('topgrad must be 4D or 5D tensor')
        if output.type.ndim not in (4, 5):
            raise TypeError('output must be 4D or 5D tensor')

        if (kern.type.ndim != topgrad.type.ndim or
                kern.type.ndim != output.type.ndim):
            raise TypeError("The number of dimensions of "
                            "kern, topgrad and output must match")

        if kern.type.ndim == 5 and self.algo in ['fft', 'deterministic']:
            raise ValueError("convolution algo %s can't be used for "
                             "3d convolutions", (self.algo,))

        if (not isinstance(desc.type, CDataType) or
                desc.type.ctype != 'cudnnConvolutionDescriptor_t'):
            raise TypeError('desc must be cudnnConvolutionDescriptor_t')

        alpha = ensure_dt(alpha, _one, 'alpha', kern.dtype)
        beta = ensure_dt(beta, _zero, 'beta', kern.dtype)

        return Apply(self, [kern, topgrad, output, desc, alpha, beta],
                     [output.type()])

    def infer_shape(self, node, shape):
        return [shape[2]]


def dnn_conv(img, kerns, border_mode='valid', subsample=(1, 1),
             conv_mode='conv', direction_hint=None, workmem=None,
             algo=None):
    """
    GPU convolution using cuDNN from NVIDIA.

    The memory layout to use is 'bc01', that is 'batch', 'channel',
    'first dim', 'second dim' in that order.

    Parameters
    ----------
    img
        Images to do the convolution over.
    kerns
        Convolution filters.
    border_mode
        One of 'valid', 'full'; additionally, the padding size
        could be directly specified by an integer or a pair of integers.
    subsample
        Perform subsampling of the output (default: (1, 1)).
    conv_mode
        Perform convolution (kernels flipped) or cross-correlation.
        One of 'conv', 'cross' (default: 'conv').
    direction_hint
        Used by graph optimizers to change algorithm choice.
        By default, GpuDnnConv will be used to carry out the convolution.
        If border_mode is 'valid', subsample is (1, 1) and direction_hint is
        'bprop weights', it will use GpuDnnConvGradW.
        If border_mode is 'full', subsample is (1, 1) and direction_hint is
        *not* 'forward!', it will use GpuDnnConvGradI.
        This parameter is used internally by graph optimizers and may be
        removed at any time without a deprecation period. You have been warned.
    algo : {'none', 'small', 'large', 'fft', 'guess_once', 'guess_on_shape_change', 'time_once', 'time_on_shape_change'}
        Convolution implementation to use. Some of its values may
        require certain versions of CuDNN to be installed. Default is
        the value of :attr:`config.dnn.conv.algo_fwd`.

    .. warning:: The cuDNN library only works with GPUs that have a compute
        capability of 3.0 or higer. This means that older GPUs will not
        work with this Op.

    """
    if workmem is not None:
        if algo is not None:
            raise ValueError("You can't use both algo and workmem")
        warnings.warn("workmem is deprecated, use algo instead", stacklevel=2)
        algo = workmem
    fgraph = getattr(img, 'fgraph', None) or getattr(kerns, 'fgraph', None)
    if (border_mode == 'valid' and subsample == (1, 1) and
            direction_hint == 'bprop weights'):
        # Special case: We are asked to use GpuDnnConvGradW. We need to set
        # up a suitable 'fake' convolution to compute the gradient for.
        img = gpu_contiguous(img.dimshuffle(1, 0, 2, 3))
        if conv_mode == 'conv':
            # We need to flip manually. These 'kerns' are not the kernels
            # that would be flipped by conv_mode='conv' in GpuDnnConvGradW.
            kerns = kerns[:, :, ::-1, ::-1]
        kerns = gpu_contiguous(kerns.dimshuffle(1, 0, 2, 3))
        shape2 = shape_i(img, 2, fgraph) - shape_i(kerns, 2, fgraph) + 1
        shape3 = shape_i(img, 3, fgraph) - shape_i(kerns, 3, fgraph) + 1
        out = GpuAllocEmpty(img.dtype)(shape_i(kerns, 1, fgraph),
                                       shape_i(img, 1, fgraph), shape2, shape3)
        desc = GpuDnnConvDesc(border_mode='valid', subsample=(1, 1),
                              conv_mode='cross')(out.shape)
        conv = GpuDnnConvGradW()(img, kerns, out, desc)
        return as_gpuarray_variable(conv.dimshuffle(1, 0, 2, 3))

    elif (border_mode == 'full' and subsample == (1, 1) and
          direction_hint != 'forward!'):
        # Special case: We can be faster by using GpuDnnConvGradI to compute
        # the full convolution as the backward pass of a valid convolution.
        # We just need to set up a suitable 'fake' valid convolution.
        img = gpu_contiguous(img)  # cudnn v2 rc3 need contiguous data
        kerns = gpu_contiguous(kerns.dimshuffle(1, 0, 2, 3))
        conv_mode = 'cross' if conv_mode == 'conv' else 'conv'
        shape2 = shape_i(img, 2, fgraph) + shape_i(kerns, 2, fgraph) - 1
        shape3 = shape_i(img, 3, fgraph) + shape_i(kerns, 3, fgraph) - 1
        out = GpuAllocEmpty(img.dtype)(shape_i(img, 0, fgraph),
                                       shape_i(kerns, 1, fgraph),
                                       shape2, shape3)
        desc = GpuDnnConvDesc(border_mode='valid', subsample=(1, 1),
                              conv_mode=conv_mode)(kerns.shape)
        return GpuDnnConvGradI()(kerns, img, out, desc)

    # Standard case: We use GpuDnnConv with suitable padding.
    # contig_version will return a gpu_contiguous copy
    # if the img contains negative strides
    img = gpu_contiguous(img)
    kerns = gpu_contiguous(kerns)
    desc = GpuDnnConvDesc(border_mode=border_mode, subsample=subsample,
                          conv_mode=conv_mode)(kerns.shape)
    desc_op = desc.owner.op
    out_shp = GpuDnnConv.get_out_shape(img.shape, kerns.shape,
                                       desc_op.border_mode,
                                       desc_op.subsample)
    out = GpuAllocEmpty(img.dtype)(*out_shp)
    return GpuDnnConv(algo=algo)(img, kerns, out, desc)


class GpuDnnPoolDesc(Op):
    """
    This Op builds a pooling descriptor for use in the other
    pooling operations.

    `ws`, `stride` and `pad` must have the same length.

    Parameters
    ----------
    ws : tuple
        Window size.
    stride : tuple
        (dx, dy) or (dx, dy, dz).
    mode : {'max', 'average_inc_pad', 'average_exc_pad'}
        The old deprecated name 'average' corresponds to 'average_inc_pad'.
    pad : tuple
        (padX, padY) or (padX, padY, padZ)

    """

    __props__ = ('ws', 'stride', 'mode', 'pad')

    def c_headers(self):
        return ['cudnn.h', 'cudnn_helper.h']

    def c_header_dirs(self):
        return [os.path.dirname(__file__), config.dnn.include_path]

    def c_libraries(self):
        return ['cudnn']

    def c_lib_dirs(self):
        return [config.dnn.library_path]

    def do_constant_folding(self, node):
        return False

    def __init__(self, ws=(1, 1), stride=(1, 1), mode='max', pad=(0, 0)):
        if mode == 'average':
            mode = 'average_inc_pad'
        assert mode in ('max', 'average_inc_pad', 'average_exc_pad')
        self.mode = mode

        assert len(ws) == len(stride) and len(stride) == len(pad)
        assert len(ws) in (2, 3)
        self.ws = ws
        self.stride = stride
        self.pad = pad

        if self.get_ndim() == 3 and version() < 3000:
            raise RuntimeError("CuDNN 3d pooling requires v3")

    def get_ndim(self):
        return len(self.ws)

    def __setstate__(self, d):
        self.__dict__.update(d)
        if not hasattr(self, 'pad'):
            self.pad = (0, 0)

    def make_node(self):
        return Apply(self, [],
                     [CDataType("cudnnPoolingDescriptor_t",
                                freefunc="cudnnDestroyPoolingDescriptor")()])

    def c_code(self, node, name, inputs, outputs, sub):
        desc, = outputs

        if self.mode == 'max':
            mode_flag = 'CUDNN_POOLING_MAX'
        elif self.mode == "average_inc_pad":
            mode_flag = 'CUDNN_POOLING_AVERAGE_COUNT_INCLUDE_PADDING'
        elif self.mode == "average_exc_pad":
            mode_flag = 'CUDNN_POOLING_AVERAGE_COUNT_EXCLUDE_PADDING'
        else:
            raise NotImplementedError("Unsupported pooling model.")

        return """
{
  cudnnStatus_t err;

  if ((err = cudnnCreatePoolingDescriptor(&%(desc)s)) != CUDNN_STATUS_SUCCESS) {
    PyErr_Format(PyExc_MemoryError, "could not allocate pooling "
                 "descriptor: %%s", cudnnGetErrorString(err));
    %(fail)s
  }

  static const int win[%(nd)d] = {%(win)s};
  static const int pad[%(nd)d] = {%(pad)s};
  static const int str[%(nd)d] = {%(str)s};
  err = cudnnSetPoolingNdDescriptor(
    %(desc)s, %(mode_flag)s, %(nd)d,
    win, pad, str);
  if (err != CUDNN_STATUS_SUCCESS) {
    PyErr_Format(PyExc_RuntimeError, "could not set op descriptor: %%s",
                 cudnnGetErrorString(err));
    %(fail)s
  }
}
""" % dict(name=name, desc=desc, mode_flag=mode_flag, fail=sub['fail'],
           nd=self.get_ndim(), win=', '.join(map(str, self.ws)),
           pad=', '.join(map(str, self.pad)),
           str=', '.join(map(str, self.stride)))

    def c_code_cache_version(self):
        return (3, version())


class GpuDnnPool(DnnBase):
    """
    Pooling.

    Parameters
    ----------
    img
        The image 4d tensor.
    desc
        The pooling descriptor.

    """

    __props__ = ()

    def __init__(self):
        DnnBase.__init__(self, ["dnn_pool.c"], "APPLY_SPECIFIC(dnn_pool)")

    def make_node(self, img, desc):
        img = as_gpuarray_variable(img)

        if desc.owner is not None:
            e_ndim = desc.owner.op.get_ndim() + 2

            if img.type.ndim != e_ndim:
                raise TypeError('img must be %dD tensor' % (e_ndim,))

        if (not isinstance(desc.type, CDataType) or
                desc.type.ctype != 'cudnnPoolingDescriptor_t'):
            raise TypeError('desc must be cudnnPoolingDescriptor_t')

        return Apply(self, [img, desc], [img.type()])

    def infer_shape(self, node, shape):
        desc = node.inputs[1].owner.op
        w = desc.ws
        s = desc.stride
        p = desc.pad
        res = [shape[0][0], shape[0][1],
               (shape[0][2] + 2 * p[0] - w[0]) // s[0] + 1,
               (shape[0][3] + 2 * p[1] - w[1]) // s[1] + 1
               ]
        if len(w) > 2:
            res.append((shape[0][4] + 2 * p[2] - w[2]) // s[2] + 1)
        return [res]

    def grad(self, inp, grads):
        img, desc = inp
        grad, = grads

        grad = gpu_contiguous(grad)

        out = self(img, desc)

        g_out = GpuDnnPoolGrad()(img, out, grad, desc)

        return g_out, theano.gradient.DisconnectedType()()

    def connection_pattern(self, node):
        # not connected to desc
        return [[1], [0]]


class GpuDnnPoolGrad(DnnBase):
    """
    The pooling gradient.

    Parameters
    ----------
    inp
        The input of the pooling.
    out
        The output of the pooling in the forward.
    inp_grad
        Same size as out, but is the corresponding gradient information.
    desc
        The pooling descriptor.

    """

    __props__ = ()

    def __init__(self):
        DnnBase.__init__(self, ["dnn_pool_grad.c"],
                         "APPLY_SPECIFIC(dnn_pool_grad)")

    def make_node(self, inp, out, out_grad, desc):
        inp = as_gpuarray_variable(inp)
        out_grad = as_gpuarray_variable(out_grad)
        out = as_gpuarray_variable(out)

        if desc.owner is not None:
            nd = desc.owner.op.get_ndim() + 2

            if inp.type.ndim != nd:
                raise TypeError('inp must be %dD tensor' % (nd,))

            if out_grad.type.ndim != nd:
                raise TypeError('out_grad must be %dD tensor' % (nd,))

            if out.type.ndim != nd:
                raise TypeError('out must be %dD tensor' % (nd,))

        if (not isinstance(desc.type, CDataType) or
                desc.type.ctype != 'cudnnPoolingDescriptor_t'):
            raise TypeError('desc must be cudnnPoolingDescriptor_t')

        return Apply(self, [inp, out, out_grad, desc], [inp.type()])

    def infer_shape(self, node, shape):
        return [shape[0]]


def dnn_pool(img, ws, stride=(1, 1), mode='max', pad=(0, 0)):
    """
    GPU pooling using cuDNN from NVIDIA.

    The memory layout to use is 'bc01', that is 'batch', 'channel',
    'first dim', 'second dim' in that order.

    `ws`, `stride` and `pad` must have the same length.

    Parameters
    ----------
    img
        Images to do the pooling over.
    ws : tuple
        Subsampling window size.
    stride : tuple
        Subsampling stride (default: (1, 1)).
    mode : {'max', 'average_inc_pad', 'average_exc_pad'}
    pad : tuple
        (padX, padY) or (padX, padY, padZ)
        default: (0, 0)

    .. warning:: The cuDNN library only works with GPU that have a compute
        capability of 3.0 or higer.  This means that older GPU will not
        work with this Op.

    Notes
    -----
    This Op implements the ignore_border=True of max_pool_2d.

    """
    img = gpu_contiguous(img)
    desc = GpuDnnPoolDesc(ws=ws, stride=stride, mode=mode, pad=pad)()
    return GpuDnnPool()(img, desc)


class GpuDnnSoftmaxBase(DnnBase):
    """
    Op for the cuDNN Softmax.

    Parameters
    ----------
    algo
        'fast' or 'accurate' indicating whether computations should be
        optimized for speed or accuracy respectively.
    mode
        'instance' or 'channel' indicating whether the softmax should be
        computed per image across 'c01' or per spatial location '01' per
        image across 'c'.

    """

    __props__ = ('mode', 'algo')

    def __init__(self, algo, mode):
        DnnBase.__init__(self, [self.file], self.c_func)

        assert(algo in ('fast', 'accurate', 'log'))
        if algo == 'log' and version() < 3000:
            raise RuntimeError("Need CuDNN v3 for log-softmax")
        self.algo = algo

        assert(mode in ('instance', 'channel'))
        self.mode = mode

    def infer_shape(self, node, shape):
        if self.direction == 'forward':
            return [shape[0]]
        else:
            return [shape[1]]

    def get_op_params(self):
        if self.mode == 'instance':
            mode = "CUDNN_SOFTMAX_MODE_INSTANCE"
        else:
            mode = "CUDNN_SOFTMAX_MODE_CHANNEL"

        if self.algo == 'fast':
            algo = "CUDNN_SOFTMAX_FAST"
        elif self.algo == 'log':
            algo = "CUDNN_SOFTMAX_LOG"
        else:
            algo = "CUDNN_SOFTMAX_ACCURATE"

        return [("SOFTMAX_MODE", mode), ("SOFTMAX_ALGO", algo)]


class GpuDnnSoftmax(GpuDnnSoftmaxBase):
    """
    Op for the cuDNN Softmax.

    algo
        'fast' or 'accurate' indicating whether computations should be
        optimized for speed or accuracy respectively.
    mode
        'instance' or 'channel' indicating whether the softmax should be
        computed per image across 'c01' or per spatial location '01' per
        image across 'c'.

    """
    direction = "forward"
    file = "dnn_softmax.c"
    c_func = "APPLY_SPECIFIC(softmax)"

    def make_node(self, x):
        x = as_gpuarray_variable(x)
        assert x.ndim == 4
        return Apply(self, [x], [x.type()])

    def grad(self, inp, grads):
        x, = inp
        g_sm, = grads
        sm = self.make_node(x).outputs[0]
        return [GpuDnnSoftmaxGrad(
                self.algo,
                self.mode
                )(g_sm, sm)]


class GpuDnnSoftmaxGrad(GpuDnnSoftmaxBase):
    """
    Op for the cuDNN SoftmaxGrad.

    Parameters
    ----------
    algo
        'fast' or 'accurate' indicating whether computations should be
        optimized for speed or accuracy respectively.
    mode
        'instance' or 'channel' indicating whether the softmax should
        be computed per image across 'c01' or per spatial location '01' per
        image across 'c'.

    """
    direction = 'backward'
    file = "dnn_softmax_grad.c"
    c_func = "APPLY_SPECIFIC(softmax_grad)"

    def make_node(self, dy, sm):
        dy = as_gpuarray_variable(dy)
        sm = as_gpuarray_variable(sm)
        assert dy.ndim == 4
        assert sm.ndim == 4
        return Apply(self, [dy, sm], [sm.type()])


# @register_opt('cudnn')  # this optimizer is registered in opt.py instead.
@local_optimizer([GpuConv])
def local_conv_dnn(node):
    if not dnn_available():
        return
    if isinstance(node.op, GpuConv):
        if node.op.border_mode not in ['full', 'valid']:
            return
        img, kern = node.inputs
        border_mode = node.op.border_mode
        subsample = node.op.subsample
        direction_hint = node.op.direction_hint
        rval = dnn_conv(img, kern,
                        border_mode=border_mode, subsample=subsample,
                        direction_hint=direction_hint)
        return [rval]


# This optimizer is registered in opt.py as part of the meta-optimizer.
# It tries exactly the opposite code path of what local_conv_dnn() uses,
# because for some input/kernel shape configurations, this is faster.
@local_optimizer([GpuConv])
def local_conv_dnn_alternative(node):
    if not dnn_available():
        return
    if isinstance(node.op, GpuConv):
        border_mode = node.op.border_mode
        subsample = node.op.subsample
        if border_mode not in ['full', 'valid'] or subsample != (1, 1):
            return
        img, kern = node.inputs
        direction_hint = node.op.direction_hint
        if border_mode == 'full':
            # for a full convolution, try using the forward pass instead
            # of the backward pass wrt. inputs
            direction_hint = 'forward!'
        elif border_mode == 'valid':
            # for a valid convolution, try using the backward pass wrt.
            # weights instead of the forward pass and vice versa
            if direction_hint == 'bprop weights':
                direction_hint = 'forward'
            else:
                direction_hint = 'bprop weights'
        rval = dnn_conv(img, kern,
                        border_mode=border_mode, subsample=subsample,
                        direction_hint=direction_hint)
        return [rval]


conv_groupopt.register('local_conv_dnn', local_conv_dnn, 20,
                       'conv_dnn', 'fast_compile', 'fast_run', 'cudnn')


@local_optimizer([GpuDnnConv], inplace=True)
def local_dnn_conv_inplace(node):
    if type(node.op) != GpuDnnConv or node.op.inplace:
        return
    inputs = list(node.inputs)
    dest = inputs[2]
    if (dest.owner and
            isinstance(dest.owner.op, GpuAllocEmpty) and
            len(dest.clients) > 1):
        inputs[2] = GpuAllocEmpty(dest.owner.op.dtype)(*dest.owner.inputs)
    return [GpuDnnConv(algo=node.op.algo, inplace=True)(*inputs)]


@local_optimizer([GpuDnnConvGradW], inplace=True)
def local_dnn_convgw_inplace(node):
    if type(node.op) != GpuDnnConvGradW or node.op.inplace:
        return
    inputs = list(node.inputs)
    dest = inputs[2]
    if (dest.owner and
            isinstance(dest.owner.op, GpuAllocEmpty) and
            len(dest.clients) > 1):
        inputs[2] = GpuAllocEmpty(dest.owner.op.dtype)(*dest.owner.inputs)
    return [GpuDnnConvGradW(algo=node.op.algo, inplace=True)(*inputs)]


@local_optimizer([GpuDnnConvGradI], inplace=True)
def local_dnn_convgi_inplace(node):
    if type(node.op) != GpuDnnConvGradI or node.op.inplace:
        return
    inputs = list(node.inputs)
    dest = inputs[2]
    if (dest.owner and
            isinstance(dest.owner.op, GpuAllocEmpty) and
            len(dest.clients) > 1):
        inputs[2] = GpuAllocEmpty(dest.owner.op.dtype)(*dest.owner.inputs)
    return [GpuDnnConvGradI(algo=node.op.algo, inplace=True)(*inputs)]

optdb.register('local_dnna_conv_inplace',
               tensor.opt.in2out(local_dnn_conv_inplace,
                                 local_dnn_convgw_inplace,
                                 local_dnn_convgi_inplace,
                                 name="local_dnn_conv_inplace"),
               70.0, 'fast_run', 'inplace', 'gpuarray', 'cudnn')


@register_opt('cudnn')
@alpha_merge(GpuDnnConv, alpha_in=4, beta_in=5, nd=4)
def local_dnn_conv_alpha_merge(node, *inputs):
    return [GpuDnnConv(algo=node.op.algo)(*inputs)]


@register_opt('cudnn')
@alpha_merge(GpuDnnConvGradW, alpha_in=4, beta_in=5, nd=4)
def local_dnn_convw_alpha_merge(node, *inputs):
    return [GpuDnnConvGradW(algo=node.op.algo)(*inputs)]


@register_opt('cudnn')
@alpha_merge(GpuDnnConvGradI, alpha_in=4, beta_in=5, nd=4)
def local_dnn_convi_alpha_merge(node, *inputs):
    return [GpuDnnConvGradI(algo=node.op.algo)(*inputs)]


@register_opt('cudnn')
@output_merge(GpuDnnConv, alpha_in=4, beta_in=5, out_in=2, nd=4)
def local_dnn_conv_output_merge(node, *inputs):
    inputs = inputs[0:2] + (gpu_contiguous(inputs[2]),) + inputs[3:]
    return [GpuDnnConv(algo=node.op.algo)(*inputs)]


@register_opt('cudnn')
@output_merge(GpuDnnConvGradW, alpha_in=4, beta_in=5, out_in=2, nd=4)
def local_dnn_convw_output_merge(node, *inputs):
    inputs = inputs[0:2] + (gpu_contiguous(inputs[2]),) + inputs[3:]
    return [GpuDnnConvGradW(algo=node.op.algo)(*inputs)]


@register_opt('cudnn')
@output_merge(GpuDnnConvGradI, alpha_in=4, beta_in=5, out_in=2, nd=4)
def local_dnn_convi_output_merge(node, *inputs):
    inputs = inputs[0:2] + (gpu_contiguous(inputs[2]),) + inputs[3:]
    return [GpuDnnConvGradI(algo=node.op.algo)(*inputs)]


@register_opt('cudnn')
@op_lifter([DownsampleFactorMax])
def local_pool_dnn_alternative(node):
    if not dnn_available():
        return
    if not node.op.ignore_border:
        return
    img, = node.inputs
    ds = node.op.ds
    stride = node.op.st
    pad = node.op.padding
    mode = node.op.mode
    return dnn_pool(gpu_contiguous(img.owner.inputs[0]),
                    ds, stride=stride, pad=pad, mode=mode)


@register_opt('cudnn')
@op_lifter([MaxPoolGrad])
def local_pool_dnn_grad_stride(node):
    if not dnn_available():
        return
    if not node.op.ignore_border:
        return
    inp, out, out_grad = node.inputs
    ds = node.op.ds
    st = node.op.st
    pad = node.op.padding
    mode = node.op.mode

    desc = GpuDnnPoolDesc(ws=ds, stride=st, mode=mode, pad=pad)()
    return GpuDnnPoolGrad()(gpu_contiguous(inp),
                            gpu_contiguous(out),
                            gpu_contiguous(out_grad),
                            desc)


@register_opt('cudnn')
@op_lifter([AveragePoolGrad])
def local_avg_pool_dnn_grad_stride(node):
    if not dnn_available():
        return
    if not node.op.ignore_border:
        return
    inp, out_grad = node.inputs
    ds = node.op.ds
    st = node.op.st
    pad = node.op.padding
    mode = node.op.mode

    cg = gpu_contiguous(out_grad)

    desc = GpuDnnPoolDesc(ws=ds, stride=st, mode=mode, pad=pad)()
    # We reuse cg because CuDNN does not use the value of the `out`
    # argument but still checks its shape for average pooling. This
    # has been observed in v2 and v3 as far as I know.
    return GpuDnnPoolGrad()(gpu_contiguous(inp), cg, cg, desc)


@register_opt('cudnn')
@local_optimizer([GpuSoftmax])
def local_softmax_dnn(node):
    if not dnn_available():
        return
    if isinstance(node.op, GpuSoftmax):
        ins = node.inputs[0].dimshuffle(0, 1, 'x', 'x')
        ins = gpu_contiguous(ins)
        out = GpuDnnSoftmax('accurate', 'channel')(ins)
        out = as_gpuarray_variable(out.dimshuffle(0, 1))
        return [out]


@register_opt('cudnn')
@local_optimizer([GpuElemwise])
def local_log_softmax_dnn(node):
    if not dnn_available() or version() < 3000:
        # No log-softmax before cudnn v3
        return
    if (isinstance(node.op, GpuElemwise) and
            isinstance(node.op.scalar_op, Log) and
            node.inputs[0].owner and
            isinstance(node.inputs[0].owner.op, GpuDnnSoftmax) and
            len(node.inputs[0].clients) == 1):
        softmax_node = node.inputs[0].owner
        new_softmax = GpuDnnSoftmax('log', softmax_node.op.mode)
        return [new_softmax(softmax_node.inputs[0])]


class NoCuDNNRaise(Optimizer):
    def apply(self, fgraph):
        """
        Raise a RuntimeError if cudnn can't be used.

        """
        if not dnn_available():
            # Make an assert error as we want Theano to fail, not
            # just skip this optimization.
            raise AssertionError(
                "cuDNN optimization was enabled, but Theano was not able"
                " to use it. We got this error: \n" +
                dnn_available.msg)

gpu_seqopt.register("NoCuDNNRaise", NoCuDNNRaise(), 0, 'cudnn')


@register_opt('cudnn')
@op_lifter([SoftmaxGrad])
def local_softmax_dnn_grad(node):
    if not dnn_available():
        return
    ins = []
    for n in node.inputs:
        if isinstance(n.owner.op, HostFromGpu):
            n = n.owner.inputs[0]
        if n.ndim != 2:
            return
        ins.append(n.dimshuffle(0, 1, 'x', 'x'))

    out = GpuDnnSoftmaxGrad('accurate', 'channel')(
        gpu_contiguous(ins[0]), gpu_contiguous(ins[1]))
    return [out.dimshuffle(0, 1)]
