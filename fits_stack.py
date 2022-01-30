#!/usr/bin/env python
# coding: utf-8

##############################################################
# Stack the images in fits files.
# Define input parameters
##############################################################

import os, imageio, cv2, time, sys, matplotlib

import scipy.fft as fft
import numpy as np
import multiprocessing as mp
import matplotlib.pyplot as plt

from scipy import ndimage
from scipy.signal.windows import tukey
from datetime import datetime
from astropy.io import fits
from astropy.coordinates import SkyCoord, EarthLocation, AltAz
from astropy.time import Time as astro_time
from astropy.time import TimezoneInfo
import astropy.units as u

# longitude, latitude, height and time zone of the AHU observatory
# and define the location object of the AHU observatory
ahu_lon = 117.21    # needs to be updated
ahu_lat = 31.84     # needs to be updated
ahu_height = 63.00  # needs to be updated
ahu_zone = +8
ahu_observatory = EarthLocation(lon=ahu_lon*u.deg, lat=ahu_lat*u.deg, height=ahu_height*u.m)

# observation target, either in name or in ra-dec
# in name, use (for example): target = SkyCoord.from_name("m31"); 
# in ra-dec, use (for example): target = SkyCoord(ra=45*u.deg, dec=45*u.deg)
target = SkyCoord.from_name("m42")

# Working directory, all raw or fits files should be in this directory
working_dir = "/work/astro/fits"

# Specify whether or not to fix the field rotation (needs the observation time, target and site locations). 
# For an Alt-az mount, this is necessary, but for an equatorial mount this is unnecessary.
do_fix_ratation = True

# Name of the final image
final_file = "final.tiff"

# Working precision takes effect in FFT and matrix multiplication
working_precision = "float64"

# Define the input file extension. All files in the working directory with this extension will be used.
extension = "fit"

# Page number of data in the fits file
page_num = 0

# Tukey window alpha parameter, used to improve the mathced filtering
# for example, 0.04 means 2% at each edge (left, right, top, bottom) is suppressed
tukey_alpha = 0.04

# Tag for the obs. date and time string (for fits file)
date_tag = 'DATE-OBS'

# Define the Bayer matrix format, only for the fits file
bayer_matrix_format = cv2.COLOR_BayerBG2RGB

# If true, work in console mode, will not process the Jupyter notebook code and will not produce online images.
console = True

# Define the maximum number of processes to be used
nproc_max = 96

# If true, do not report the alignment result
less_report = True

# Fraction of frames that will not be used
bad_fraction = 0.3

# dark_fac means the fraction of pixels that will be ignored as "too dark".
dark_frac = 5e-4

# bright_frac means the fraction of pixels that will be ignored as "too bright".
bright_frac = 5e-4

# The red, green and blue pixels are amplified by this factor for a custom white balance.
rgb_fac = [1.05, 0.90, 0.4]

# Final gamma
rgb_gamma = [0.5,0.5,0.5]

# Save aligned binary files or not. Note that for multiprocessing, this must be True
save_aligned_binary = False

# Save aligned images?
save_aligned_image = False

# Number of ADC digit. The true maximum value should be 2**adc_digit. This should usualy be 16.
adc_digit_max = 16

# field rotation ang file
file_rot_ang = "field_rot_ang.bin"













##############################################################
# Define subroutines, no computation here.
##############################################################
#
# read fits file and convert to bin so it can be used by multiprocessing returns: frame, time
#
def read_frame_fits(file):
    with fits.open(file) as hdu:
        frame = hdu[page_num].data
        raw_data_type = frame.dtype
        hdr = hdu[page_num].header
        date_str = hdr[date_tag]
    return frame, date_str, raw_data_type


#
# use the fits information to convert a frame to rgb image
#
def frame2rgb_fits(frame):
    rgb = cv2.cvtColor(frame.astype(raw_data_type), bayer_matrix_format)
    return rgb


# 
# align a frame to the reference frame
# 
def align_frames(i):
    tst = time.time()
    # read the raw data as an object, obtain the image and compute its fft
    frame = np.fromfile(file_swp[i], dtype=raw_data_type).reshape(n1, n2)
    frame_fft = fft.fft2((frame*win).astype(working_precision))
    
    # compute the frame offset
    cache = np.abs(fft.ifft2(ref_fft*frame_fft))
    index = np.unravel_index(np.argmax(cache, axis=None), cache.shape)
    s1, s2 = -index[0], -index[1]
    
    # make sure that the Bayer matrix will not be corrupted
    s1 = s1 - np.mod(s1, 2)
    s2 = s2 - np.mod(s2, 2)
    
    # fix the offset and save into the result array
    frame = np.roll(frame, (s1, s2), axis=(0,1))
    
    # save the aligned images and binaries if necessary
    frame.tofile(file_swp[i])
    
    if less_report==False:
        print("\nFrame %6i (%s) aligned in %8.2f sec, (sx, sy) = (%8i,%8i)." %(i, file_lst[i], time.time()-tst, s1, s2))
    return i, s1, s2


def compute_weights(frames_working):
    # read the alignment results of multiple processes
    tst = time.time()
    for i in range(n_files):
        frame = np.fromfile(file_swp[i], dtype=raw_data_type)
        frames_working[i,:,:] = frame.reshape(n1, n2)

    print("Aligned frames read in, time cost:                        %9.2f" %(time.time()-tst)); tst = time.time()

    # remove the mean value from each frame
    tst = time.time()
    for i in range(0, n_files):
        frames_working[i,:,:] = frames_working[i,:,:] - np.mean(frames_working[i,:,:])
    print("Mean values of frames removed, time cost:                 %9.2f" %(time.time()-tst)); tst = time.time()
    
    # compute the covariance matrix
    frames_working = frames_working.reshape(n_files, n1*n2)
    cov = np.dot(frames_working, frames_working.transpose())

    # compute weights from the covariance matrix
    w = np.zeros(n_files)
    for i in range(n_files):
        w[i] = np.sum(cov[i,:])/cov[i,i] - 1

    return w


import heapq as hp
# subroutine for adjusting the colors 
def adjust_color(i, m1, m2, bin_file, raw_data_type):
    # number of "too dark" pixels and threshold
    val_max = 2.**adc_digit_max
    samp = np.fromfile(bin_file, dtype=raw_data_type).reshape(m1*m2)
    npix = np.int64(m1)*np.int64(m2)
    ndark = int(npix*dark_frac)
    d1 = hp.nsmallest(ndark, samp)[ndark-1]*1.
    # number of "too bright" pixels and threshold
    nbright = int(npix*bright_frac)
    d2 = hp.nlargest(nbright, samp)[nbright-1]*1.
    # rescaling, note that this requires 16-bit to save weak signal from bright sky-light
    # note that val is expected to be in range [0,1]. Out-of-range values will be truncated.
    val = np.float64(samp-d1)/np.float64(d2-d1)
    val = np.where(val<=0, 1./val_max, val)
    val = np.where(val >1, 1, val)
    samp = (val**rgb_gamma[i])*val_max*rgb_fac[i]
    samp = np.where(samp<      0,       0, samp)
    samp = np.where(samp>val_max, val_max, samp)
    samp.astype(raw_data_type).tofile(bin_file)


# convert elevation and azimuth to unit vectors
def elaz2vec(el, az):
    n = np.size(el)
    d2r = np.pi/180
    vec = np.zeros([n, 3])
    vec[:,0] = np.cos(el*d2r)*np.cos(az*d2r)
    vec[:,1] = np.cos(el*d2r)*np.sin(az*d2r)
    vec[:,2] = np.sin(el*d2r)
    return vec


# compute the field rotation
def compute_field_rot(target, hor_ref_frame):
    north_pole = SkyCoord(ra=0*u.deg, dec=90*u.deg)
    north_pole_coord_hor = north_pole.transform_to(hor_ref_frame)
    target_coord_hor = target.transform_to(hor_ref_frame)
    # convert el-az to unit vectors
    el_target = target_coord_hor.alt.to_value()
    az_target = target_coord_hor.az.to_value()
    el_np = north_pole_coord_hor.alt.to_value()
    az_np = north_pole_coord_hor.az.to_value()
    vec_target = elaz2vec(el_target, az_target)
    vec_north = elaz2vec(el_np, az_np)
    vec_z = vec_north*0; vec_z[:,2] = 1
    # compute the local east by cross product for the two reference frames respectively
    vec1 = np.cross(vec_target, vec_north)
    vec2 = np.cross(vec_target, vec_z)
    # the field rotation is the angle between the two "local east"
    amp1 = np.sqrt( np.sum(vec1*vec1, axis=1) )
    amp2 = np.sqrt( np.sum(vec2*vec2, axis=1) )
    rot_ang = np.arccos( np.sum(vec1*vec2, axis=1)/amp1/amp2 )*180/np.pi
    return rot_ang

# note: need to check if we should multiply -1 to angle.
def fix_rotation(file, angle, raw_data_type, n1, n2):
    frame = np.fromfile(file, dtype=raw_data_type).reshape(int(n1/2), 2, int(n2/2), 2)
    frame00 = frame[:,0,:,0].reshape(int(n1/2), int(n2/2))
    frame01 = frame[:,0,:,1].reshape(int(n1/2), int(n2/2))
    frame10 = frame[:,1,:,0].reshape(int(n1/2), int(n2/2))
    frame11 = frame[:,1,:,1].reshape(int(n1/2), int(n2/2))
    frame00 = ndimage.rotate(frame00, angle, reshape=False)
    frame01 = ndimage.rotate(frame01, angle, reshape=False)
    frame10 = ndimage.rotate(frame10, angle, reshape=False)
    frame11 = ndimage.rotate(frame11, angle, reshape=False)
    frame00 = np.where(frame00==0, np.median(frame00), frame00)
    frame01 = np.where(frame01==0, np.median(frame01), frame01)
    frame10 = np.where(frame10==0, np.median(frame10), frame10)
    frame11 = np.where(frame11==0, np.median(frame11), frame11)
    frame[:,0,:,0] = frame00
    frame[:,0,:,1] = frame01
    frame[:,1,:,0] = frame10
    frame[:,1,:,1] = frame11
    frame.tofile(file)

    
    
    
    
    

    


##############################################################
# Do the following:
# 1. Align the frames using the initial reference frame.
# 2. Compute the weights from covarinace matrix.
# 3. Set the reference frame to the one with highest weight.
# 4. Align the frames again using the new reference frame.
# 5. Re-compute the weights from covarinace matrix.
# 6. Stack with weights.
# 7. Adjust the color
##############################################################

if console == False:
    # Improve the display effect of Jupyter notebook
    from IPython.core.display import display, HTML
    display(HTML("<style>.container { width:95% !important; }</style>"))
else:
    # do not produce online images (but will still save pdf)
    matplotlib.use('Agg')

# make a list of woking files and determine the number of processes to be used
os.chdir(working_dir)
file_lst, file_bin, file_swp, file_tif = [], [], [], []
for file in os.listdir():
    if file.endswith(extension):
        file_lst.append(file)

n_files = np.int64(len(file_lst))
if nproc_max > n_files:
    nproc = n_files
else:
    nproc = nproc_max
    
# sort the file list and then build auxiliary file lists accordingly
file_lst.sort()
for file in file_lst:
    file_swp.append(os.path.splitext(file)[0] + '.swp')
    file_bin.append(os.path.splitext(file)[0] + '.bin')
    file_tif.append(os.path.splitext(file)[0] + '.tif')

# use the first file as the initial reference file
ref_frame, _, raw_data_type = read_frame_fits(file_lst[0]) 
n1 = np.int64(np.shape(ref_frame)[0])
n2 = np.int64(np.shape(ref_frame)[1])

# make the 2D-tukey window
w1 = tukey(n1, alpha=tukey_alpha)
w2 = tukey(n2, alpha=tukey_alpha)
win = np.dot(w1.reshape(n1, 1), w2.reshape(1, n2))

# read the frames by main
if __name__ == '__main__':
    # prepare the working array
    frames_working = np.zeros([n_files, n1, n2], dtype=working_precision)
    
    # read frames, save the observation times, and compute the local horizontal reference frames accordingly
    tst = time.time()
    datetime = []
    for i in range(n_files):
        frame1, time1, _ = read_frame_fits(file_lst[i])
        frame1.tofile(file_swp[i])
        datetime.append(time1)
        #####################
        frames_working[i,:,:] = frame1
    print("Frames read and cached by the main proc, time cost:       %9.2f" %(time.time()-tst))

# fix rotation if necessary (multi-processes)
if __name__ == '__main__':
    if do_fix_ratation==True:
        tst = time.time()
        obstime_list = astro_time(datetime) - ahu_zone*u.hour
        hor_ref_frame = AltAz(obstime=obstime_list, location=ahu_observatory)

        # compute the reletive time in seconds, and subtract the median value to minimize the rotations
        rel_sec = (obstime_list - obstime_list[0]).to_value(format='sec')
        rel_sec = rel_sec - np.median(rel_sec)
        
        # compute the absolute field rotation angles as "rot_ang"
        rot_ang = compute_field_rot(target, hor_ref_frame)
        rot_ang = rot_ang - np.median(rot_ang)
        rot_ang.astype(working_precision).tofile(file_rot_ang)
        print("Rotation angles computed, time cost:                      %9.2f" %(time.time()-tst))

        # plot the field rotation angle for test 
        plt.figure(figsize=(4,2),dpi=200)
        plt.title('The field rotation angle')
        plt.xlabel('Time (sec)',fontsize=9)
        plt.ylabel('Angle',fontsize=9)
        plt.plot(rel_sec, rot_ang, marker="o")
        plt.savefig('field_rot_angle.pdf')

        # fix the field rotation (multi-processes)
        tst = time.time()
        p1, p2, p3, p4, p5 = [], [], [], [], []
        for i in range(n_files):
            p1.append(file_swp[i])
            p2.append(rot_ang[i])
            p3.append(raw_data_type)
            p4.append(n1)
            p5.append(n2)
        with mp.Pool(nproc) as pool:
            output = [pool.starmap(fix_rotation, zip(p1, p2, p3, p4, p5))]
        print("Field rotation fixed, time cost:                          %9.2f" %(time.time()-tst) )

# For all processes: if fix-rotation is required, then read rot_ang from file and 
# reset the reference frame to the one with least rotation (frame already fixed)
if do_fix_ratation==True:
    rot_ang = np.fromfile(file_rot_ang, dtype=working_precision)
    wid = np.argmin(np.abs(rot_ang))
    ref_frame = np.fromfile(file_swp[wid], dtype=raw_data_type).reshape(n1, n2)
    ref_fft = np.conjugate(fft.fft2((ref_frame*win).astype(working_precision)))
    
if __name__ == '__main__':    
    tst = time.time()
    with mp.Pool(nproc) as pool:
        output = [pool.map(align_frames, range(n_files))]
    print("Initial alignment done, time cost:                        %9.2f" %(time.time()-tst))

    # identify the frame of maximum weight, and use it as the new reference frame.
    w = compute_weights(frames_working)
    wid = np.argmax(w)
    ref_frame = np.fromfile(file_swp[wid], dtype=raw_data_type).reshape(n1, n2)
    ref_fft = np.conjugate(fft.fft2((ref_frame*win).astype(working_precision)))
    print("****************************************************")
    print("Frame %i is chosen as the new reference frame. All frames will be re-aligned." %(wid))
    print("The new reference file is: %s" %(file_lst[wid]))
    print("****************************************************")
    
    # work with multiprocessing to align the frames again, and remove the swp files
    tst = time.time()
    with mp.Pool(nproc) as pool:
        output = [pool.map(align_frames, range(n_files))]
    print("Final alignment done, time cost:                          %9.2f" %(time.time()-tst))
    
    # parse and record the offsets
    output_arr = np.array(output)
    sx, sy = output_arr[0,:,1], output_arr[0,:,2]
    sx = np.where(sx >  n1/2, sx-n1, sx)
    sx = np.where(sx < -n1/2, sx+n1, sx)
    sy = np.where(sy >  n2/2, sy-n2, sy)
    sy = np.where(sy < -n2/2, sy+n2, sy)
    
    # recompute the weights
    tst = time.time()
    w = compute_weights(frames_working)
    # exclude the low quality frames
    n_bad = int(n_files*bad_fraction)
    thr = hp.nsmallest(n_bad, w)[n_bad-1]
    if thr<0: thr = 0
    w = np.where(w <= thr, 0, w)
    w = w / np.sum(w)
    print("Final weights computed, time cost:                        %9.2f" %(time.time()-tst))
    
    for file in file_swp:
        os.remove(file)
    if do_fix_ratation==True:
        os.remove(file_rot_ang)
    
    # plot the weights for test 
    plt.figure(figsize=(4,2),dpi=200)
    plt.title(r'Stacking weights ($w\times N_{frames}$)')
    plt.xlabel('Frame number',fontsize=9)
    plt.ylabel(r'$w\times N_{frames}$',fontsize=9)
    w1 = np.where(w==0, np.nan, w)
    w2 = np.where(w==0, np.median(w), np.nan)
    plt.plot(w1*n_files, marker="o", label='Valid')
    plt.plot(w2*n_files, marker="*", label='Invalid')
    plt.legend()
    plt.savefig('weights.pdf')
    
    # plot the XY-shifts
    plt.figure(figsize=(4,2),dpi=200)
    plt.title('XY shifts in pixel')
    plt.xlabel('Y shifts',fontsize=9)
    plt.ylabel('X shifts',fontsize=9)
    plt.scatter(sx, sy, s=50, alpha=.5)
    plt.savefig('xy-shifts.pdf')

    # stack the frames with weights.
    tst = time.time()
    frame_stacked = np.dot(w, frames_working.reshape(n_files, n1*n2)).reshape(n1, n2)
    # normalize the stacked result to 0-65535.
    fmin = np.amin(frame_stacked)
    fmax = np.amax(frame_stacked)
    cache = (frame_stacked-fmin)/(fmax-fmin)
    tmax = 2.**(adc_digit_max) - 1.
    frame_stacked = np.floor(cache*tmax)
    print("Stacked frame obtained from %i/%i best frames, time cost:  %9.2f" 
        %(n_files-n_bad, n_files, time.time()-tst))


    # adjust the color and make the final 8-bit image
    tst = time.time()
    rgb = frame2rgb_fits(frame_stacked)
    r_bin_file = os.path.splitext(final_file)[0] + '.r'; rgb[:,:,0].tofile(r_bin_file)
    g_bin_file = os.path.splitext(final_file)[0] + '.g'; rgb[:,:,1].tofile(g_bin_file)
    b_bin_file = os.path.splitext(final_file)[0] + '.b'; rgb[:,:,2].tofile(b_bin_file)
    
    # color correction in parallel
    m1, m2, npix = np.shape(rgb)[0], np.shape(rgb)[1], rgb.size
    tst = time.time()
    ic = [0, 1, 2]
    im1 = [m1, m1, m1]
    im2 = [m2, m2, m2]
    ifn = [r_bin_file, g_bin_file, b_bin_file]
    dtp = [raw_data_type, raw_data_type, raw_data_type]
    with mp.Pool(3) as pool:
        output = [pool.starmap(adjust_color, zip(ic, im1, im2, ifn, dtp))]

    # read the color correction result and save to 
    rgb[:,:,0] = np.fromfile(r_bin_file, dtype=raw_data_type).reshape(m1, m2); os.remove(r_bin_file)
    rgb[:,:,1] = np.fromfile(g_bin_file, dtype=raw_data_type).reshape(m1, m2); os.remove(g_bin_file)
    rgb[:,:,2] = np.fromfile(b_bin_file, dtype=raw_data_type).reshape(m1, m2); os.remove(b_bin_file)
    print("Color adjusted, time cost:                                %9.2f" %(time.time()-tst)); tst = time.time()
    

    # save the final figure
    imageio.imsave(final_file, np.uint8(rgb))

    # show the final figure
    plt.figure(figsize=(6,4),dpi=200)
    plt.xlabel('Y',fontsize=12)
    plt.ylabel('X',fontsize=12)
    plt.imshow(np.uint8(rgb/256))

    print("Done!")

