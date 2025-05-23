# Copyright 2021 United States Government as represented by the Administrator of the National Aeronautics and Space
# Administration.  No copyright is claimed in the United States under Title 17, U.S. Code. All Other Rights Reserved.


r"""
This module provides a subclass of :class:`.CameraModel` that implements the OpenCV camera model, which adds
distortion corrections to the Pinhole model.

Theory
------

The OpenCV camera model is the pinhole camera model combined with a lens distortion model which projects any point
along a ray emanating from the camera center (origin of the camera frame) to the same 2D point in an image.  Given
some 3D point (or direction) expressed in the camera frame, :math:`\mathbf{x}_C`, the OpenCV model is defined as

.. math::
    &\mathbf{x}_I = \frac{1}{z_C}\left[\begin{array}{c} x_C \\ y_C \end{array}\right] \\
    &r = \sqrt{x_I^2 + y_I^2} \\
    &\mathbf{x}_I' = \frac{1+k_1r^2+k_2r^4+k_3r^6}{1+k_4r^2+k_5r^4+k_6r^6}\mathbf{x}_I +
    \left[\begin{array}{c} 2p_1x_Iy_I+p_2(r^2+2x_I^2)+s1r^2+s2r^4 \\
    p_1(r^2+2y_I^2) + 2p_2x_Iy_I +s3r^2+s4r^4\end{array}\right] \\
    &\mathbf{x}_P = \left[\begin{array}{ccc} f_x & \alpha & p_x \\ 0 & f_y & p_y\end{array}\right]
    \left[\begin{array}{c} (1+a_1T+a_2T^2+a_3T^3)\mathbf{x}_I' \\ 1 \end{array}\right]

where :math:`\mathbf{x}_I` are the image frame coordinates for the point (pinhole location), :math:`r` is the
radial distance from the principal point of the camera to the gnomic location of the point, :math:`k_{1-6}`
are radial distortion coefficients, :math:`p_{1-2}` are tip/tilt/prism distortion coefficients, :math:`s_{1-4}` are
thin prism distortion coefficients,
:math:`\mathbf{x}_I'` is the distortion for point :math:`\mathbf{x}_I`, :math:`f_x` and :math:`f_y` are the
focal length divided by the pixel pitch in the :math:`x` and :math:`y` directions respectively experessed in units
of pixels, :math:`\alpha` is an alpha term for non-rectangular pixels, :math:`p_x` and :math:`p_y` are the location
of the principal point of the camera in the image expressed in units of pixels, :math:`T` is the temperature of the
camera, :math:`a_{1-3}` are temperature dependence coefficients, and :math:`\mathbf{x}_P` is the pixel location of
the point in the image. For a more thorough description of the OpenCV camera model checkout
:download:`this memo <docs/cameraModels.pdf>`.

Speeding up the camera model
----------------------------

One of the most common functions of the camera model is to relate pixels in a camera to unit vectors in the 3D camera
frame.  This is done extensively throughout GIANT, particularly when ray tracing.  Unfortunately, this transformation is
iterative (there isn't an analytic solution), which can make things a little slow, particularly when you need to do the
transformation for many pixel locations.

In order to speed up this transformation we can precompute it for each pixel in an detector and for a range of
temperatures specified by a user and then use bilinear interpolation to compute the location of future pixel/temperature
combinations we need.  While this is an approximation, it saves significant time rather than going through the full
iterative transformation, and based on testing, it is accurate to a few thousandths of a pixel, which is more than
sufficient for nearly every use case.  The :class:`.OpenCVModel` and its subclasses make precomputing the
transformation, and using the precomputed transformation, as easy as calling :meth:`~OpenCVModel.prepare_interp`
once.  Future calls to any method that then needs the transformation from pixels to gnomic locations (on the way to
unit vectors) will then use the precomputed transformation unless specifically requested otherwise.  In addition,
once the :meth:`~OpenCVModel.prepare_interp` method has been called, if the resulting camera object is then saved to
a file either using the :mod:`.camera_model`
:func:`~giant.camera_models.camera_model.save`/:func:`~giant.camera_models.camera_model.load` functions  or another
serialization method like pickle/dill, then the precomputed transformation will also be saved and loaded so that it
truly only needs to be computed once.

Since precomputing the transformation can take a somewhat long time, it is not always smart to do so.  Typically if you
have a camera model that you will be using again and again (as is typical in most operations and analysis cases) then
you *should* precompute the transformation and save the resulting camera object to a file that is then used for future
work.  This is usually best done at the end of a calibration script (for a real camera) or in a stand-alone script that
defines the camera, precomputes the transformation, and then saves it off for a synthetic camera for analysis.  If you
are just doing a quick analysis and don't need the camera model repeatedly or for any heavy duty ray tracing then it is
recommended that you *not precompute* the transformation.

Whether you precompute the transformation or not, the use of the camera model should appear unchanged beyond computation
time.

Use
___

This is a concrete implementation of a :class:`.CameraModel`, therefore to use this class you simply need to initialize
it with the proper values.  Typically these values come from either the physical dimensions of the camera, or from
a camera calibration routine performed to refine the values using observed data (see the :mod:`.calibration` sub-package
for details).  For instance, say we have a camera which has an effective focal length of 10 mm, a pix pitch of 2.2 um,
and a detector size of 1024x1024, with radial distortion of 1e-5, -2e-7, 3e-9.  We could then create a model for this
camera as

    >>> from giant.camera_models import OpenCVModel
    >>> model = OpenCVModel(fx=10/2.2e-3, fy=10/2.2e-3,
    ...                     n_rows=1024, n_cols=1024, px=(1024-1)/2, py=(1024-1)/2,
    ...                     k1=1e-2, k2=-2e-3, k3=3e-4)

Note that we did not set the field of view, but it is automatically computed for us based off of the prescribed camera
model.

    >>> model.field_of_view
    9.048754127234737

In addition, we can now use our model to project points

    >>> model.project_onto_image([0, 0, 1])
    array([511.5, 511.5])

or to determine the unit vector through a pixel

    >>> model.pixels_to_unit([[0, 500], [0, 100]])
    array([[-0.11110425, -0.00251948],
           [-0.11110425, -0.09015368],
           [ 0.9875787 ,  0.99592468]]
"""

from typing import Sequence, Union

import numpy as np

from giant.camera_models.brown_model import BrownModel
from giant.rotations import Rotation
from giant._typing import NONENUM, NONEARRAY, ARRAY_LIKE, Real


class OpenCVModel(BrownModel):
    r"""
    This class provides an implementation of the OpenCV camera model for projecting 3D points onto images and performing
    camera calibration.

    The OpenCV camera model is the pinhole camera model combined with a lens distortion model which projects any point
    along a ray emanating from the camera center (origin of the camera frame) to the same 2D point in an image.  Given
    some 3D point (or direction) expressed in the camera frame, :math:`\mathbf{x}_C`, the OpenCV model is defined as

    The :class:`OpenCVModel` class is a subclasss of :class:`CameraModel`.  This means that it includes implementations
    for all of the abstract methods defined in the :class:`CameraModel` class.  This also means that it can be used
    throughout GIANT as the primary camera model, including within the :mod:`calibration` subpackage.  If this class is
    going to be used with the :mod:`calibration` subpackage, the user can set which parameters are estimated and which
    are held fixed by using the ``estimation_parameters`` key word argument when creating an instance of the class or by
    adjusting the :attr:`estimation_parameters` instance variable on an instance of the class.  The
    ``estimation_parameters`` input/attribute is a string or list of strings specifying which parameters to estimate.
    This means that :attr:`estimation_parameters` could be something like ``'basic'`` which would indicate to estimate
    just the usual parameters, or something like ``['focal_length', 'ky', 'px', 'py']`` to estimate just the terms
    included in the list.

    In addition to the standard set of methods for a :class:`CameraModel` subclass, the :class:`OpenCVModel` class
    provides the following additional methods which may or may not be useful to some people:

    =================================  =================================================================================
    Method                             Use
    =================================  =================================================================================
    :meth:`get_projections`            computes the pinhole, image frame, and pixel locations of a 3D point
    :meth:`pixels_to_gnomic`           removes distortion from a point to get the corresponding unitless pinhole
                                       location
    =================================  =================================================================================

    The :class:`OpenCVModel` class also provides the following properties for easy getting/setting:

    ================================ ===================================================================================
    Property                         Description
    ================================ ===================================================================================
    :attr:`field_of_view`            the diagonal field of view of the camera in units of degrees
    :attr:`kx`, :attr:`fx`           :math:`f_x`, focal length divided by the pixel pitch in the x direction in units of
                                     pixels
    :attr:`ky`, :attr:`fy`           :math:`f_y`, focal length divided by the pixel pitch in the y direction in units of
                                     pixels
    :attr:`kxy`, :attr:`alpha`       :math:`\alpha`, A alpha term for non-retangular pixels
    :attr:`px`                       :math:`p_{x}`, the x axis pixel location of the principal point of the camera in
                                     units of pixels
    :attr:`py`                       :math:`p_{y}`, the y axis pixel location of the principal point of the camera in
                                     units of pixels
    :attr:`radial2n`, :attr:`k1`     :math:`k_1`, the radial distortion coefficient corresponding to :math:`r^2` in the
                                     numerator
    :attr:`radial4n`, :attr:`k2`     :math:`k_2`, the radial distortion coefficient corresponding to :math:`r^4` in the
                                     numerator
    :attr:`radial6n`, :attr:`k3`     :math:`k_3`, the radial distortion coefficient corresponding to :math:`r^6` in the
                                     numerator
    :attr:`radial2d`, :attr:`k4`     :math:`k_4`, the radial distortion coefficient corresponding to :math:`r^2` in the
                                     denominator
    :attr:`radial4d`, :attr:`k5`     :math:`k_5`, the radial distortion coefficient corresponding to :math:`r^4` in the
                                     denominator
    :attr:`radial6d`, :attr:`k6`     :math:`k_6`, the radial distortion coefficient corresponding to :math:`r^6` in the
                                     denominator
    :attr:`tiptilt_y`, :attr:`p1`    :math:`p_1`, the tip/tilt/prism distortion coefficient corresponding to
                                     :math:`y_I`
    :attr:`tiptilt_x`, :attr:`p2`    :math:`p_2`, the tip/tilt/prism distortion coefficient corresponding to
                                     :math:`x_I`
    :attr:`thinprism_1`, :attr:`s1`  :math:`s_1`, the first thin prism coefficient
    :attr:`thinprism_2`, :attr:`s2`  :math:`s_1`, the second thin prism coefficient
    :attr:`thinprism_3`, :attr:`s3`  :math:`s_1`, the third thin prism coefficient
    :attr:`thinprism_4`, :attr:`s4`  :math:`s_1`, the fourth thin prism coefficient
    :attr:`a1`                       :math:`a_1`, the linear coefficient for focal length dependent focal length
    :attr:`a2`                       :math:`a_2`, the quadratic coefficient for focal length dependent focal length
    :attr:`a3`                       :math:`a_3`, the cubic coefficient for focal length dependent focal length
    :attr:`intrinsic_matrix_inv`     The inverse of the intrinsic matrix
    ================================ ===================================================================================

    .. note:: The distortion attributes are aliases over each other and refer to the same data.  Therefore setting a
              value to :attr:`radial2n` would also change the value of :attr:`k1`

    """

    def __init__(self, intrinsic_matrix: NONEARRAY = None, fx: NONENUM = None, fy: NONENUM = None, px: NONENUM = None,
                 py: NONENUM = None, alpha: NONENUM = None, kx: NONENUM = None, ky: NONENUM = None, kxy: NONENUM = None,
                 field_of_view: NONENUM = None, use_a_priori: bool = False,
                 distortion_coefficients: NONEARRAY = None,
                 k1: NONENUM = None, k2: NONENUM = None, k3: NONENUM = None,
                 radial2n: NONENUM = None, radial4n: NONENUM = None, radial6n: NONENUM = None,
                 k4: NONENUM = None, k5: NONENUM = None, k6: NONENUM = None,
                 radial2d: NONENUM = None, radial4d: NONENUM = None, radial6d: NONENUM = None,
                 p1: NONENUM = None, p2: NONENUM = None, tiptilt_y: NONENUM = None, tiptilt_x: NONENUM = None,
                 s1: NONENUM = None, s2: NONENUM = None, s3: NONENUM = None, s4: NONENUM = None,
                 thinprism_1: NONENUM = None, thinprism_2: NONENUM = None,
                 thinprism_3: NONENUM = None, thinprism_4: NONENUM = None,
                 temperature_coefficients: NONEARRAY = None, a1: NONENUM = None, a2: NONENUM = None, a3: NONENUM = None,
                 misalignment: NONEARRAY = None,
                 estimation_parameters: Union[str, Sequence] = 'basic', n_rows: int = 1, n_cols: int = 1):
        """
        :param intrinsic_matrix: the intrinsic matrix for the camera as a numpy shape (2, 3) array.  Note that this is
                                 overwritten if ``kx``, ``ky``, ``kxy``, ``kyx``, ``px``, ``py``, ``fx``, ``fy``,
                                 ``alpha`` are also specified.
        :param field_of_view: The field of view of the camera in units of degrees.
        :param use_a_priori: A flag to indicate whether to include the a priori state vector in the Jacobian matrix when
                            performing a calibration
        :param distortion_coefficients: A numpy array of shape (12,) containing the twelve distortion coefficients in
                                        order [k1, k2, k3, k4, k5, k6, p1, p2, s1, s2, s3, s4].
                                        Note that this array is overwritten with any distortion coefficients that are
                                        specified independently.
        :param misalignment: either a numpy array of shape (3,) or a list of numpy arrays of shape(3,) with each array
                             corresponding to a single image (the list of numpy arrays is only valid when estimating
                             multiple misalignments)
        :param estimation_parameters: A string or list of strings specifying which model parameters to include in the
                                      calibration
        :param fx: The pixel pitch along the x axis in units of pixels
        :param fy: The pixel pitch along the y axis in units of pixels
        :param kx: The pixel pitch along the x axis in units of pixels
        :param ky: The pixel pitch along the y axis in units of pixels
        :param kxy: A alpha term for non-rectangular pixels
        :param alpha: An alpha term for non-rectangular pixels
        :param px: the x component of the pixel location of the principal point in the image in units of pixels
        :param py: the y component of the pixel location of the principal point in the image in units of pixels
        :param radial2n: the radial distortion coefficient corresponding to the r**2 term in the numerator
        :param radial4n: the radial distortion coefficient corresponding to the r**4 term in the numerator
        :param radial6n: the radial distortion coefficient corresponding to the r**4 term in the numerator
        :param radial2d: the radial distortion coefficient corresponding to the r**2 term in the denominator
        :param radial4d: the radial distortion coefficient corresponding to the r**4 term in the denominator
        :param radial6d: the radial distortion coefficient corresponding to the r**4 term in the denominator
        :param k1: the radial distortion coefficient corresponding to the r**2 term in the numerator
        :param k2: the radial distortion coefficient corresponding to the r**4 term in the numerator
        :param k3: the radial distortion coefficient corresponding to the r**4 term in the numerator
        :param k4: the radial distortion coefficient corresponding to the r**2 term in the denominator
        :param k5: the radial distortion coefficient corresponding to the r**4 term in the denominator
        :param k6: the radial distortion coefficient corresponding to the r**4 term in the denominator
        :param tiptilt_y: the tip/tilt/decentering distortion coefficient corresponding to the y term
        :param tiptilt_x: the tip/tilt/decentering distortion coefficient corresponding to the x term
        :param p1: the tip/tilt/decentering distortion coefficient corresponding to the y term
        :param p2: the tip/tilt/decentering distortion coefficient corresponding to the x term
        :param s1: The first thin prism distortion coefficient
        :param s2: The second thin prism distortion coefficient
        :param s3: The third thin prism distortion coefficient
        :param s4: The fourth thin prism distortion coefficient
        :param thinprism_1: The first thin prism distortion coefficient
        :param thinprism_2: The second thin prism distortion coefficient
        :param thinprism_3: The third thin prism distortion coefficient
        :param thinprism_4: The fourth thin prism distortion coefficient
        :param n_rows: the number of rows of the active image array
        :param n_cols: the number of columns in the active image array
        """

        super().__init__(intrinsic_matrix=intrinsic_matrix,
                         kx=kx, ky=ky, kxy=kxy, fx=fx, fy=fy, alpha=alpha, px=px, py=py,
                         temperature_coefficients=temperature_coefficients, a1=a1, a2=a2, a3=a3,
                         misalignment=misalignment, use_a_priori=use_a_priori,
                         estimation_parameters=estimation_parameters, n_rows=n_rows, n_cols=n_cols)

        # set the distortion coefficients vector
        self.distortion_coefficients = np.zeros(12)
        """
        The distortion coefficients array contains the distortion coefficients for the OpenCV model 
        [k1, k2, k3, k4, k5, k6, p1, p2, s1, s2, s3, s4]
        """

        if distortion_coefficients is not None:
            self.distortion_coefficients = distortion_coefficients

        if k1 is not None:
            self.k1 = k1
        if k2 is not None:
            self.k2 = k2
        if k3 is not None:
            self.k3 = k3
        if k4 is not None:
            self.k4 = k4
        if k5 is not None:
            self.k5 = k5
        if k6 is not None:
            self.k6 = k6
        if p1 is not None:
            self.p1 = p1
        if p2 is not None:
            self.p2 = p2
        if s1 is not None:
            self.s1 = s1
        if s2 is not None:
            self.s2 = s2
        if s3 is not None:
            self.s3 = s3
        if s4 is not None:
            self.s4 = s4

        if radial2n is not None:
            self.radial2n = radial2n
        if radial4n is not None:
            self.radial4n = radial4n
        if radial6n is not None:
            self.radial6n = radial6n
        if radial2d is not None:
            self.radial2d = radial2d
        if radial4d is not None:
            self.radial4d = radial4d
        if radial6d is not None:
            self.radial6d = radial6d
        if tiptilt_y is not None:
            self.tiptilt_y = tiptilt_y
        if tiptilt_x is not None:
            self.tiptilt_x = tiptilt_x
        if thinprism_1 is not None:
            self.thinprism_1 = thinprism_1
        if thinprism_2 is not None:
            self.thinprism_2 = thinprism_2
        if thinprism_3 is not None:
            self.thinprism_3 = thinprism_3
        if thinprism_4 is not None:
            self.thinprism_4 = thinprism_4

        self._state_labels = ['fx', 'fy', 'alpha', 'px', 'py', 'k1', 'k2', 'k3', 'k4', 'k5', 'k6', 'p1', 'p2',
                              's1', 's2', 's3', 's4', 'a1', 'a2', 'a3', 'misalignment']
        """
        A list of state labels that correspond to the attributes of this class.
        """

        # store the element dict for indexing the update vector and Jacobian matrix
        self.element_dict = {
            'basic': [0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 21, 22],
            'intrinsic': np.arange(0, 17),
            'no prism': np.arange(0, 12),
            'basic intrinsic': [0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
            'temperature dependence': [17, 18, 19],
            'fx': [0],
            'kx': [0],
            'fy': [1],
            'ky': [1],
            'alpha': [2],
            'kxy': [2],
            'px': [3],
            'py': [4],
            'k1': [5],
            'k2': [6],
            'k3': [7],
            'k4': [8],
            'k5': [9],
            'k6': [10],
            'p1': [11],
            'p2': [12],
            's1': [13],
            's2': [14],
            's3': [15],
            's4': [16],
            'radial2n': [5],
            'radial4n': [6],
            'radial6n': [7],
            'radial2d': [8],
            'radial4d': [9],
            'radial6d': [10],
            'tiptilt_y': [11],
            'tiptilt_x': [12],
            'thinprism_1': [13],
            'thinprism_2': [14],
            'thinprism_3': [15],
            'thinprism_4': [16],
            'a1': [17],
            'a2': [18],
            'a3': [19],
            'single misalignment': slice(20, None, None),
            'multiple misalignments': slice(20, None, None)
        }

        # add the distortion parameters to the important variables
        self.important_attributes = self.important_attributes + ['k4', 'k5', 'k6', 's1', 's2', 's3', 's4']
        """
        A list specifying the important attributes the must be saved/loaded for this camera model to be completely 
        reconstructed. 
        """

        self.field_of_view = field_of_view

    def __repr__(self):

        template = "OpenCVModel(fx={fx}, fy={fy}, px={px}, py={py}, alpha={skew},\n" \
                   "            field_of_view={fov}, k1={k1}, k2={k2}, k3={k3}, k4={k4}, k5={k5}, k6={k6},\n" \
                   "            p1={p1}, p2={p2}, s1={s1}, s2={s2}, s3={s3}, s4={s4},\n" \
                   "            misalignment={mis!r}, a1={a1}, a2={a2}, a3={a3}, n_rows={nr}, n_cols={nc},\n" \
                   "            estimation_parameters={ep!r}, use_a_priori={ap})\n\n"

        return template.format(
            fx=self.fx, fy=self.fy, px=self.px, py=self.py, skew=self.alpha,
            fov=self.field_of_view, k1=self.k1, k2=self.k2, k3=self.k3, p1=self.p1, p2=self.p2, mis=self.misalignment,
            ep=self.estimation_parameters, ap=self.use_a_priori, a1=self.a1, a2=self.a2, a3=self.a3, nr=self.n_rows,
            nc=self.n_cols, k4=self.k4, k5=self.k5, k6=self.k6, s1=self.s1, s2=self.s2, s3=self.s3, s4=self.s4
        )

    def __str__(self):

        template = u"Open CV Model: \n\n" \
                   u" __ __       __     __     \n" \
                   u"|  x  |  =  |  Xc/Zc  |    \n" \
                   u"|  y  |     |  Yc/Zc  |    \n" \
                   u" -- --       --     --     \n" \
                   u"                              \n" \
                   u"         ________             \n" \
                   u"        /  2   2              \n" \
                   u"  r = \\/  x + y               \n" \
                   u"                               \n" \
                   u" _  _              2      4       6    _ _     _               2    2 _     _    2       4 _  \n" \
                   u"| x' |     (1 + k1*r + k2*r  + k3*r ) | x |   | 2p1*x*y + p2*(r + 2x ) |   | s1*r  + s2*r   | \n" \
                   u"|    | =  ––––––––––––––––––––––––––––|   | + |                        | + |                | \n" \
                   u"|    |              2      4       6  |   |   |     2    2             |   |     2       4  | \n" \
                   u"| y' |     (1 + k4*r + k5*r  + k6*r ) | y |   | p1(r + 2y ) + 2p2*x*y  |   | s3*r  + s4*r   | \n" \
                   u" -  -                                  - -     -                      -     -              -  \n" \
                   u" __  __                               __ __   \n" \
                   u"|  xt' |                   2      3  |  x' |  \n" \
                   u"|      | = (1 + a1*T + a2*T + a3*T ) |     |  \n" \
                   u"|  yt' |                             |  y' |  \n" \
                   u" --  --                               -- --   \n" \
                   u" __ __     __             __  __  __       \n" \
                   u"|  u  | _ |  fx  alpha  px  ||  xt' |      \n" \
                   u"|  v  | - |  0    fy    py  ||  yt' |      \n" \
                   u" -- --     --             -- |  1   |      \n" \
                   u"                              --  --      \n\n" \
                   u"————————————————————————————————————————————————————————————————————————————\n\n" \
                   u"distortion coefficients:\n" \
                   u"    k1={k1}, k2={k2}, k3={k3}, k4={k4}, k5={k5}, k6={k6}, p1={p1}, p2={p2},\n" \
                   u"    s1={s1}, s2={s2}, s3={s3} s4={s4}\n\n" \
                   u"camera parameters:\n" \
                   u"    fx={fx}, fy={fy}, alpha={skew}, px={px}, py={py}\n\n" \
                   u"temperature coefficients:\n" \
                   u"    a1={a1}, a2={a2}, a3={a3}\n\n"

        return template.format(
            fx=self.fx, fy=self.fy, px=self.px, py=self.py, skew=self.alpha,
            k1=self.k1, k2=self.k2, k3=self.k3, k4=self.k4, k5=self.k5, k6=self.k6, p1=self.p1, p2=self.p2,
            s1=self.s1, s2=self.s2, s3=self.s3, s4=self.s4, a1=self.a1, a2=self.a2, a3=self.a3
        )

    @property
    def k1(self) -> float:
        """
        The radial distortion coefficient corresponding to the r**2 term in the numeratore

        This corresponds to the [0] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[0]

    @k1.setter
    def k1(self, val):
        self.distortion_coefficients[0] = val

    @property
    def k2(self) -> float:
        """
        The radial distortion coefficient corresponding to the r**4 term in the numerator.

        This corresponds to the [1] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[1]

    @k2.setter
    def k2(self, val):
        self.distortion_coefficients[1] = val

    @property
    def k3(self) -> float:
        """
        The radial distortion coefficient corresponding to the r**6 term in the numerator.

        This corresponds to the [2] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[2]

    @k3.setter
    def k3(self, val):
        self.distortion_coefficients[2] = val

    @property
    def k4(self) -> float:
        """
        The radial distortion coefficient corresponding to the r**2 term in the denominator

        This corresponds to the [3] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[3]

    @k4.setter
    def k4(self, val):
        self.distortion_coefficients[3] = val

    @property
    def k5(self) -> float:
        """
        The radial distortion coefficient corresponding to the r**4 term in the denominator

        This corresponds to the [4] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[4]

    @k5.setter
    def k5(self, val):
        self.distortion_coefficients[4] = val

    @property
    def k6(self) -> float:
        """
        The radial distortion coefficient corresponding to the r**6 term in the denominator

        This corresponds to the [5] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[5]

    @k6.setter
    def k6(self, val):
        self.distortion_coefficients[5] = val

    @property
    def p1(self) -> float:
        """
        The tip/tilt/decentering distortion coefficient corresponding to the y term.

        This corresponds to the [6] index of the distortion_coefficients array.
        """
        return self.distortion_coefficients[6]

    @p1.setter
    def p1(self, val):
        self.distortion_coefficients[6] = val

    @property
    def p2(self) -> float:
        """
        The tip/tilt/decentering distortion coefficient corresponding to the x term.

        This corresponds to the [7] index of the distortion_coefficients array.
        """
        return self.distortion_coefficients[7]

    @p2.setter
    def p2(self, val):
        self.distortion_coefficients[7] = val

    @property
    def s1(self) -> float:
        """
        The first thin prism distortion coefficient corresponding to r^2 for x.

        This corresponds to the [8] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[8]

    @s1.setter
    def s1(self, val):
        self.distortion_coefficients[8] = val

    @property
    def s2(self) -> float:
        """
        The second thin prism distortion coefficient corresponding to r^4 for x.

        This corresponds to the [9] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[9]

    @s2.setter
    def s2(self, val):
        self.distortion_coefficients[9] = val

    @property
    def s3(self) -> float:
        """
        The third thin prism distortion coefficient corresponding to r^2 for y.

        This corresponds to the [10] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[10]

    @s3.setter
    def s3(self, val):
        self.distortion_coefficients[10] = val

    @property
    def s4(self) -> float:
        """
        The fourth thin prism distortion coefficient corresponding to r^4 for y.

        This corresponds to the [11] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[11]

    @s4.setter
    def s4(self, val):
        self.distortion_coefficients[11] = val

    @property
    def radial2n(self):
        """
        The radial distortion coefficient corresponding to the r**2 term in the numeratore

        This corresponds to the [0] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[0]

    @radial2n.setter
    def radial2n(self, val):
        self.distortion_coefficients[0] = val

    @property
    def radial4n(self):
        """
        The radial distortion coefficient corresponding to the r**4 term in the numerator.

        This corresponds to the [1] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[1]

    @radial4n.setter
    def radial4n(self, val):
        self.distortion_coefficients[1] = val

    @property
    def radial6n(self):
        """
        The radial distortion coefficient corresponding to the r**6 term in the numerator.

        This corresponds to the [2] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[2]

    @radial6n.setter
    def radial6n(self, val):
        self.distortion_coefficients[2] = val

    @property
    def radial2d(self):
        """
        The radial distortion coefficient corresponding to the r**2 term in the denominator

        This corresponds to the [3] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[3]

    @radial2d.setter
    def radial2d(self, val):
        self.distortion_coefficients[3] = val

    @property
    def radial4d(self):
        """
        The radial distortion coefficient corresponding to the r**4 term in the denominator

        This corresponds to the [4] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[4]

    @radial4d.setter
    def radial4d(self, val):
        self.distortion_coefficients[4] = val

    @property
    def radial6d(self):
        """
        The radial distortion coefficient corresponding to the r**6 term in the denominator

        This corresponds to the [5] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[5]

    @radial6d.setter
    def radial6d(self, val):
        self.distortion_coefficients[5] = val

    @property
    def tiptilt_y(self):
        """
        The tip/tilt/decentering distortion coefficient corresponding to the y term.

        This corresponds to the [6] index of the distortion_coefficients array.
        """
        return self.distortion_coefficients[6]

    @tiptilt_y.setter
    def tiptilt_y(self, val):
        self.distortion_coefficients[6] = val

    @property
    def tiptilt_x(self):
        """
        The tip/tilt/decentering distortion coefficient corresponding to the x term.

        This corresponds to the [7] index of the distortion_coefficients array.
        """
        return self.distortion_coefficients[7]

    @tiptilt_x.setter
    def tiptilt_x(self, val):
        self.distortion_coefficients[7] = val

    @property
    def thinprism_1(self):
        """
        The first thin prism distortion coefficient corresponding to r^2 for x.

        This corresponds to the [8] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[8]

    @thinprism_1.setter
    def thinprism_1(self, val):
        self.distortion_coefficients[8] = val

    @property
    def thinprism_2(self):
        """
        The second thin prism distortion coefficient corresponding to r^4 for x.

        This corresponds to the [9] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[9]

    @thinprism_2.setter
    def thinprism_2(self, val):
        self.distortion_coefficients[9] = val

    @property
    def thinprism_3(self):
        """
        The third thin prism distortion coefficient corresponding to r^2 for y.

        This corresponds to the [10] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[10]

    @thinprism_3.setter
    def thinprism_3(self, val):
        self.distortion_coefficients[10] = val

    @property
    def thinprism_4(self):
        """
        The fourth thin prism distortion coefficient corresponding to r^4 for y.

        This corresponds to the [11] index of the distortion_coefficients array
        """
        return self.distortion_coefficients[11]

    @thinprism_4.setter
    def thinprism_4(self, val):
        self.distortion_coefficients[11] = val

    def apply_distortion(self, pinhole_locations: ARRAY_LIKE) -> np.ndarray:
        r"""
        This method applies the distortion model to the specified pinhole (gnomic) locations in the image frame.

        In general this function is not used by the user and the higher level :meth:`project_onto_image` is used
        which calls this method (along with a few others) instead.  In cases were it is desirable to use this method
        the pinhole locations should be input as a shape (2,) or shape (2, n) array of unitless image plane locations.
        The output from this function is the unitless distorted image plane locations.

        For the OpenCV model, the conversion from gnomic to distorted points is defined as

        .. math::
            \mathbf{x}_I' = \frac{1+k_1r^2+k_2r^4+k_3r^6}{1+k_4r^2+k_5r^4+k_6r^6}\mathbf{x}_I +
            \left[\begin{array}{c} 2p_1x_Iy_I+p_2(r^2+2x_I^2)+s1r^2+s2r^4 \\
            p_1(r^2+2y_I^2) + 2p_2x_Iy_I +s3r^2+s4r^4\end{array}\right]

        where :math:`\Delta\mathbf{x}_I'` is the distorted gnomic locations,
        :math:`k_{1-6}` are radial distortion coefficients,
        :math:`p_{1-2}` are the tangential distortion coefficients,
        :math:`s_{1-4}` are thin prism distortion coefficients,
        :math:`\mathbf{x}_I` is the unitless gnomic location,  and
        :math:`r = \sqrt{\mathbf{x}_I^T\mathbf{x}_I}` is the radial distance from the optical axis.

        :param pinhole_locations: The unitless image plane location of points to be distorted as a shape (2,) or (2, n)
                                  array.
        :return: The unitless distorted locations of the points on the image plane as a shape (2,) or (2, n) array.
        """

        # ensure we are dealing with an array
        pinhole_locations = np.array(pinhole_locations)

        # compute the powers of the radial distance from the optical axis
        radius2 = (pinhole_locations * pinhole_locations).sum(axis=0)
        radius4 = radius2 * radius2
        radius6 = radius2 * radius4

        # create aliases for easier coding
        rows = pinhole_locations[1]
        cols = pinhole_locations[0]

        # compute the product of the x and y terms
        rowscols = rows * cols

        # compute the radial distortion
        radial_numer = 1 + self.k1 * radius2 + self.k2 * radius4 + self.k3 * radius6
        radial_denom = 1 + self.k4 * radius2 + self.k5 * radius4 + self.k6 * radius6
        radial_distortion = (radial_numer / radial_denom) * pinhole_locations

        # compute the tip/tilt/decentering distortion
        decentering_distortion = np.vstack([self.p1 * 2 * rowscols + self.p2 * (radius2 + 2 * cols * cols),
                                            self.p1 * (radius2 + 2 * rows * rows) + self.p2 * 2 * rowscols])

        # compute the thin prism distortion
        prism_distortion = np.vstack([(self.s1 * radius2 + self.s2 * radius4),
                                      (self.s3 * radius2 + self.s4 * radius4)])

        if decentering_distortion.size == 2 and pinhole_locations.ndim == 1:
            decentering_distortion = decentering_distortion.ravel()
            prism_distortion = prism_distortion.ravel()

        # sum everything together
        return radial_distortion + decentering_distortion + prism_distortion

    def project_onto_image(self, points_in_camera_frame: ARRAY_LIKE, image: int = 0,
                           temperature: Real = 0) -> np.ndarray:
        """
        This method transforms 3D points or directions expressed in the camera frame into the corresponding 2D image
        locations.

        The points input should be either 1 or 2 dimensional, with the first axis being length 3 (each point
        (direction) in the camera frame is specified as a column).

        The optional ``image`` key word argument specifies the index of the image you are projecting onto (this only
        applies if you have a separate misalignment for each image)

        The optional temperature input specifies the temperature to perform the projection at.  This is only import when
        your focal length is dependent on temperature and you have entered or calibrated for the temperature dependency
        coefficients.

        You can specify the directions to be input as either a shape (3,) or shape (3, n) array::
            >>> from giant.camera_models import OpenCVModel
            >>> model = OpenCVModel(kx=3000, ky=4000, px=500, py=500, a1=1e-5, a2=1e-6,
            >>>                     misalignment=[[1e-12, -2e-14, 3e-10], [2e-15, 1e-13, 3e-10]],
            >>>                     estimation_parameters=['multiple misalignments'])
            >>> model.project_onto_image([1, 2, 12000])
            array([ 500.25      ,  500.66666667])
            >>> model.project_onto_image([[1, 2, 3, 4], [2, 5, 6, 7], [12000, 13000, 9000, 5000]], image=1)
            array([[ 500.25      ,  500.46153846,  501.        ,  502.4       ],
                   [ 500.66666667,  501.53846154,  502.66666667,  505.6       ]])
            >>> model.project_onto_image([[1, 2, 3, 4], [2, 5, 6, 7], [12000, 13000, 9000, 5000]], temperature=-1)
            array([[ 499.49999775,  499.07691892,  497.999991  ,  495.1999784 ],
                   [ 498.66666066,  496.92306307,  494.66664266,  488.79994959]])

        :param points_in_camera_frame: a shape (3,) or shape (3, n) array of points to project
        :param image: The index of the image being projected onto (only applicable with multiple misalignments)
        :param temperature: The temperature to project the points at
        :return: A shape (2,) or shape (2, n) numpy array of image points (with units of pixels)
        """

        _, __, picture_locations = self.get_projections(points_in_camera_frame, image, temperature=temperature)

        return picture_locations

    def _compute_ddistortion_dgnomic(self, gnomic: ARRAY_LIKE,
                                     radius2: float, radius4: float, radius6: float) -> np.ndarray:
        r"""
        Computes the partial derivative of the distorted gnomic location with respect to a change in the gnomic location

        Mathematically this is given by:

        .. math::
            \frac{\partial\Delta\mathbf{x}_I'}{\partial\mathbf{x}_I} = &\frac{\left(1 + k_1r^2+k_2r^4+k_3r^6\right)}
            {\left(1 + k_4r^2+k_5r^4+k_6r^6\right)}\mathbf{I}_{2\times 2} +
            \left[\begin{array}{cc}2p_1y_I+4p_2x_I & 2p_1x_I \\
            2p_2y_I & 4p_1y_I+2p_2x_I \end{array}\right] + \\
            & \frac{\left(1 + k_4r^2+k_5r^4+k_6r^6\right)\left(2k_1+4k_2r^2+6k_3r^4\right)-
            \left(1 + k_1r^2+k_2r^4+k_3r^6\right)\left(2k_4+4k_5r^2+6k_6r^4\right)}
            {\left(1 + k_4r^2+k_5r^4+k_6r^6\right)^2}\mathbf{x}_I\mathbf{x}_I^T +\\
            & 2 \left[\begin{array}{cc} p_2x_I & p_2y_I \\ p_1x_I & p_1y_I \end{array}\right] +
            2 \left[\begin{array}{cc} (s_1+2s_2r^2)x_I & (s_1+2s_2r^2)y_I \\
            (s_3+2s_4r^2)x_I & (s_3+2s_4r^2)y_I \end{array}\right]  - \mathbf{I}_{2\times 2}

        where all is defined as before

        :param gnomic: The gnomic location of the point being considered as a shape (2,) numpy array
        :param radius2: The radial distance squared from the optical axis
        :param radius4: The radial distance to the fourth power from the optical axis
        :param radius6: The radial distance to the sixth power from the optical axis
        :return: The partial derivative of the distortion with respect to a change in the gnomic location
        """

        row = gnomic[1]
        col = gnomic[0]

        radial_numer = 1 + self.k1 * radius2 + self.k2 * radius4 + self.k3 * radius6
        radial_denom = 1 + self.k4 * radius2 + self.k5 * radius4 + self.k6 * radius6

        # APPLY PRODUCT RULE
        wrt_x = ((radial_numer / radial_denom) * np.eye(2) +
                 np.array([[2 * self.p1 * row + 4 * self.p2 * col, 2 * self.p1 * col],
                           [2 * self.p2 * row, 4 * self.p1 * row + 2 * self.p2 * col]]))

        # implement chain rule for second term (X*dX'/dr*dr/dX)
        wrt_r = (((radial_denom * (2 * self.k1 + 4 * self.k2 * radius2 + 6 * self.k3 * radius4) -
                   radial_numer * (2 * self.k4 + 4 * self.k5 * radius2 + 6 * self.k6 * radius4)) /
                  (radial_denom ** 2)) *
                 np.outer(gnomic, gnomic) + 2 * np.outer([self.p2, self.p1], gnomic) +
                 2 * np.outer([(self.s1 + 2 * self.s2 * radius2), (self.s3 + 2 * self.s4 * radius2)], gnomic))

        return wrt_x + wrt_r - np.eye(2)

    def _compute_ddistorted_gnomic_dgnomic(self, gnomic: ARRAY_LIKE,
                                           radius2: float, radius4: float, radius6: float) -> np.ndarray:
        r"""
        Computes the partial derivative of the distorted gnomic location with respect to a change in the gnomic location

        Mathematically this is given by:

        .. math::
            \frac{\partial\mathbf{x}_I'}{\partial\mathbf{x}_I} = &\frac{\left(1 + k_1r^2+k_2r^4+k_3r^6\right)}
            {\left(1 + k_4r^2+k_5r^4+k_6r^6\right)}\mathbf{I}_{2\times 2} +
            \left[\begin{array}{cc}2p_1y_I+4p_2x_I & 2p_1x_I \\
            2p_2y_I & 4p_1y_I+2p_2x_I \end{array}\right] + \\
            & \frac{\left(1 + k_4r^2+k_5r^4+k_6r^6\right)\left(2k_1+4k_2r^2+6k_3r^4\right)-
            \left(1 + k_1r^2+k_2r^4+k_3r^6\right)\left(2k_4+4k_5r^2+6k_6r^4\right)}
            {\left(1 + k_4r^2+k_5r^4+k_6r^6\right)^2}\mathbf{x}_I\mathbf{x}_I^T +\\
            & 2 \left[\begin{array}{cc} p_2x_I & p_2y_I \\ p_1x_I & p_1y_I \end{array}\right] +
            2 \left[\begin{array}{cc} (s_1+2s_2r^2)x_I & (s_1+2s_2r^2)y_I \\
            (s_3+2s_4r^2)x_I & (s_3+2s_4r^2)y_I \end{array}\right]

        where all is defined as before

        :param gnomic: The gnomic location of the point being considered as a shape (2,) numpy array
        :param radius2: The radial distance squared from the optical axis
        :param radius4: The radial distance to the fourth power from the optical axis
        :param radius6: The radial distance to the sixth power from the optical axis
        :return: The partial derivative of the distortion with respect to a change in the gnomic location
        """

        row = gnomic[1]
        col = gnomic[0]

        radial_numer = 1 + self.k1 * radius2 + self.k2 * radius4 + self.k3 * radius6
        radial_denom = 1 + self.k4 * radius2 + self.k5 * radius4 + self.k6 * radius6

        # APPLY PRODUCT RULE
        wrt_x = ((radial_numer / radial_denom) * np.eye(2) +
                 np.array([[2 * self.p1 * row + 4 * self.p2 * col, 2 * self.p1 * col],
                           [2 * self.p2 * row, 4 * self.p1 * row + 2 * self.p2 * col]]))

        # implement chain rule for second term (X*dX'/dr*dr/dX)
        wrt_r = (((radial_denom * (2 * self.k1 + 4 * self.k2 * radius2 + 6 * self.k3 * radius4) -
                   radial_numer * (2 * self.k4 + 4 * self.k5 * radius2 + 6 * self.k6 * radius4)) /
                  (radial_denom ** 2)) *
                 np.outer(gnomic, gnomic) + 2 * np.outer([self.p2, self.p1], gnomic) +
                 2 * np.outer([(self.s1 + 2 * self.s2 * radius2), (self.s3 + 2 * self.s4 * radius2)], gnomic))

        return wrt_x + wrt_r

    # noinspection PyMethodOverriding
    def _compute_ddistorted_gnomic_ddistortion(self, gnomic_loc: ARRAY_LIKE,
                                               radius2: float, radius4: float, radius6: float) -> np.ndarray:
        r"""
        Computes the partial derivative of the distorted gnomic location with respect to a change in the distortion
        coefficients.

        Mathematically this is given by:

        .. math::
            u = 1+k_1r^2+k_2r^4+k_3r^6\\
            l = 1+k_4r^2+k_5r^4+k_6r^6\\
            \frac{\partial\mathbf{x}_I'}{\partial\mathbf{d}} = \left[
            \begin{array}{cccccccccccc}
            r^2x_Il^{-1} & r^4x_Il^{-1} & r^6x_Il^{-1} & -r^2x_Iul^{-2} & -r^4x_Iul^{-2} & -r^6x_Iul^{-2} &
            2x_Iy_I & r^2+2y_I^2 & r^2 & r^4 & 0 & 0 \\
            r^2y_Il^{-1} & r^4y_Il^{-1} & r^6y_Il^{-1} & -r^2y_Iul^{-2} & -r^4y_Iul^{-2} & -r^6y_Iul^{-2} &
            r^2+2x_I^2 & 2x_Iy_I & 0 & 0 & r^2 & r^4
            \end{array}
            \right]

        where
        :math:`\mathbf{d}=[\begin{array}{*{12}c} k_1 & k_2 & k_3 & k_4 & k_5 & k_6 & p_1 & p_2 & s_1 & s_2 & s_3 & s_4]`
        is a vector of the distortion coefficients and all else is as defined before.

        :param gnomic_loc: The undistorted gnomic location of the point
        :param radius2: The radial distance squared from the optical axis
        :param radius4: The radial distance to the fourth power from the optical axis
        :param radius6: The radial distance to the sixth power from the optical axis
        :return: the partial derivative of the distorted gnomic location with respect to a change in the distortion
                 coefficients
        """

        radial_numer = 1 + self.k1 * radius2 + self.k2 * radius4 + self.k3 * radius6
        radial_denom = 1 + self.k4 * radius2 + self.k5 * radius4 + self.k6 * radius6

        # Compute partial wrt radial terms
        dk1 = (radius2 / radial_denom) * gnomic_loc
        dk2 = (radius4 / radial_denom) * gnomic_loc
        dk3 = (radius6 / radial_denom) * gnomic_loc
        dk4 = -radius2 * (radial_numer / (radial_denom ** 2)) * gnomic_loc
        dk5 = -radius4 * (radial_numer / (radial_denom ** 2)) * gnomic_loc
        dk6 = -radius6 * (radial_numer / (radial_denom ** 2)) * gnomic_loc

        # Compute partial wrt to decentering terms and prism
        dp1 = [2 * gnomic_loc[0] * gnomic_loc[1], radius2 + 2 * gnomic_loc[1] ** 2]
        dp2 = [radius2 + 2 * gnomic_loc[0] ** 2, 2 * gnomic_loc[0] * gnomic_loc[1]]

        # Compute partial wrt prism terms
        ds1 = [radius2, 0]
        ds2 = [radius4, 0]
        ds3 = [0, radius2]
        ds4 = [0, radius4]

        return np.array([dk1, dk2, dk3, dk4, dk5, dk6, dp1, dp2, ds1, ds2, ds3, ds4]).T

    def apply_update(self, update_vec: ARRAY_LIKE):
        r"""
        This method takes in a delta update to camera parameters (:math:`\Delta\mathbf{c}`) and applies the update
        to the current instance in place.

        In general the delta update is calculated in the estimators in the :mod:`.calibration` subpackage and this
        method is not used by the user.

        The order of the update vector is determined by the order of the elements in
        :attr:`~.OpenCVModel.estimation_parameters`.  Any misalignment terms must always come last.

        This method operates by walking through the elements in :attr:`~.OpenCVModel.estimation_parameters` and
        retrieving the parameter that the element corresponds to.  The value of the update_vec at the index of the
        parameter is then applied as an additive update to the parameter in self, with the exception of misalignment,
        which is applied as a multiplicative update.

        :param update_vec: An iterable of delta updates to the model parameters
        """

        jacobian_parameters = np.hstack([getattr(self.element_dict[element], 'start', self.element_dict[element])
                                         for element in self.estimation_parameters])

        update_vec = self._fix_update_vector(update_vec, jacobian_parameters)

        update_vec = np.asarray(update_vec).ravel()

        for ind, parameter in enumerate(jacobian_parameters):

            if parameter == 0:
                self.intrinsic_matrix[0, 0] += update_vec.item(ind)

            elif parameter == 1:
                self.intrinsic_matrix[1, 1] += update_vec.item(ind)

            elif parameter == 2:
                self.intrinsic_matrix[0, 1] += update_vec.item(ind)

            elif parameter == 3:
                self.intrinsic_matrix[0, 2] += update_vec.item(ind)

            elif parameter == 4:
                self.intrinsic_matrix[1, 2] += update_vec.item(ind)

            elif parameter == 5:
                self.distortion_coefficients[0] += update_vec.item(ind)

            elif parameter == 6:
                self.distortion_coefficients[1] += update_vec.item(ind)

            elif parameter == 7:
                self.distortion_coefficients[2] += update_vec.item(ind)

            elif parameter == 8:
                self.distortion_coefficients[3] += update_vec.item(ind)

            elif parameter == 9:
                self.distortion_coefficients[4] += update_vec.item(ind)

            elif parameter == 10:
                self.distortion_coefficients[5] += update_vec.item(ind)

            elif parameter == 11:
                self.distortion_coefficients[6] += update_vec.item(ind)

            elif parameter == 12:
                self.distortion_coefficients[7] += update_vec.item(ind)

            elif parameter == 13:
                self.distortion_coefficients[8] += update_vec.item(ind)

            elif parameter == 14:
                self.distortion_coefficients[9] += update_vec.item(ind)

            elif parameter == 15:
                self.distortion_coefficients[10] += update_vec.item(ind)

            elif parameter == 16:
                self.distortion_coefficients[11] += update_vec.item(ind)

            elif parameter == 17:
                self.a1 += update_vec.item(ind)

            elif parameter == 18:
                self.a2 += update_vec.item(ind)

            elif parameter == 19:
                self.a3 += update_vec.item(ind)

            elif parameter == 20 or parameter == '20:':

                misalignment_updates = update_vec[ind:].reshape(3, -1, order='F')

                if self.estimate_multiple_misalignments:
                    self.misalignment = [(Rotation(update.T) * Rotation(self.misalignment[ind])).vector
                                         for ind, update in enumerate(misalignment_updates.T)]

                else:
                    self.misalignment = (
                            Rotation(misalignment_updates) * Rotation(self.misalignment)
                    ).vector

                break
