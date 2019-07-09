#!/usr/bin/env python
from __future__ import print_function
import numpy as np
import os
import time
import emcee
from astropy.io import fits
from htof.main import Astrometry

import orbit
from config import HipID, RVFile, AstrometryFile, GaiaDataDir, Hip2DataDir, Hip1DataDir

import argparse

# parse command line arguments
parser = argparse.ArgumentParser(description='Fit an orbit. Required arguments are shown without [].')
parser.add_argument("--nstep", required=False, default=2000, metavar='', type=int,
                    help="Number of MCMC steps per walker")
parser.add_argument("--nthreads", required=False, default=2, metavar='', type=int,
                    help="Number of threads to use for parallelization")
parser.add_argument("-a", "--use-epoch-astrometry", action="store_true",
                    required=False, default=False,
                    help="Whether or not to use intermediate astrometry data")
parser.add_argument("--output-dir", required=True,
                    help="Directory within which to save the MCMC results.")
parser.add_argument("--ntemps", required=False, default=5, metavar='', type=int,
                    help="number of MCMC temperatures.")
parser.add_argument("--nwalkers", required=False, default=100, metavar='', type=int,
                    help="number of MCMC walkers.")
parser.add_argument("--nplanets", required=False, default=1, metavar='', type=int,
                    help="Assumed number of planets in the system.")
parser.add_argument("--start-file", required=False, default=None, metavar='',
                    help="Filepath for the orbit initial conditions.")
args = parser.parse_args()

# initialize the following from the command line. They are overwritten later if start_file is not None.
nwalkers = args.nwalkers
ntemps = args.ntemps
nplanets = args.nplanets

######################################################################
# Garbage initial guess
######################################################################

if args.start_file is None:
    mpri = 1
    jit = 0.5
    sau = 10
    esino = 0.5
    ecoso = 0.5
    inc = 1
    asc = 1
    lam = 1
    msec = 0.1
    
    par0 = np.ones((ntemps, 100, 2 + 7*nplanets))
    init = [jit, mpri]
    for i in range(nplanets):
        init += [msec, sau, esino, ecoso, inc, asc, lam]
    par0 *= np.asarray(init)
    par0 *= 2**(np.random.rand(np.prod(par0.shape)).reshape(par0.shape) - 0.5)
    
else:

    #################################################################
    # read in the starting positions for the walkers. The next four
    # lines remove parallax and RV zero point from the optimization,
    # change semimajor axis from arcseconds to AU, and bring the
    # number of temperatures used for parallel tempering down to
    # ntemps.
    #################################################################

    par0 = fits.open(args.start_file)[0].data
    par0[:, :, 8] = par0[:, :, 9]
    par0[:, :, 9] = par0[:, :, 10]
    par0[:, :, 0] /= par0[:, :, 9]
    par0 = par0[:ntemps, :, :-2]

ntemps = par0[:, 0, 0].size
nwalkers = par0[0, :, 0].size
ndim = par0[0, 0, :].size

######################################################################
# Load in data
######################################################################

data = orbit.Data(HipID, RVFile, AstrometryFile)

if args.use_epoch_astrometry:
    Gaia_fitter = Astrometry('GaiaDR2', '%06d' % (HipID), GaiaDataDir,
                             central_epoch_ra=data.epRA_G,
                             central_epoch_dec=data.epDec_G,
                             central_epoch_fmt='frac_year')
    Hip2_fitter = Astrometry('Hip2', '%06d' % (HipID), Hip2DataDir,
                             central_epoch_ra=data.epRA_H,
                             central_epoch_dec=data.epDec_H,
                             central_epoch_fmt='frac_year')
    Hip1_fitter = Astrometry('Hip1', '%06d' % (HipID), Hip1DataDir,
                             central_epoch_ra=data.epRA_H,
                             central_epoch_dec=data.epDec_H,
                             central_epoch_fmt='frac_year')
    
    H1f = orbit.AstrometricFitter(Hip1_fitter)
    H2f = orbit.AstrometricFitter(Hip2_fitter)
    Gf = orbit.AstrometricFitter(Gaia_fitter)

    data = orbit.Data(HipID, RVFile, AstrometryFile, args.use_epoch_astrometry,
                      epochs_Hip1=Hip1_fitter.data.julian_day_epoch(),
                      epochs_Hip2=Hip2_fitter.data.julian_day_epoch(),
                      epochs_Gaia=Gaia_fitter.data.julian_day_epoch())

######################################################################
# define likelihood function for joint parameters
######################################################################


def lnprob(theta, returninfo=False):
    
    model = orbit.Model(data)

    for i in range(nplanets):
        params = orbit.Params(theta, i, nplanets)
        
        if not np.isfinite(orbit.lnprior(params)):
            model.free()
            return -np.inf

        orbit.calc_EA_RPP(data, params, model)
        orbit.calc_RV(data, params, model)
        orbit.calc_offsets(data, params, model, i)
    
    if args.use_epoch_astrometry:
        orbit.calc_PMs_epoch_astrometry(data, model, H1f, H2f, Gf)
    else:
        orbit.calc_PMs_no_epoch_astrometry(data, model)

    if returninfo:
        return orbit.calcL(data, params, model, chisq_resids=True)
        
    return orbit.lnprior(params) + orbit.calcL(data, params, model)


def return_one(theta):
    return 1.

######################################################################
# Initialize and run sampler
######################################################################

from emcee import PTSampler
kwargs = {'thin': 50}
start_time = time.time()

sample0 = emcee.PTSampler(ntemps, nwalkers, ndim, lnprob, return_one, threads=args.nthreads)
sample0.run_mcmc(par0, args.nstep, **kwargs)

print('Total Time: %.2f' % (time.time() - start_time))
print("Mean acceptance fraction (cold chain): {0:.6f}".format(np.mean(sample0.acceptance_fraction[0,:])))

shape = sample0.lnprobability[0].shape
parfit = np.zeros((shape[0], shape[1], 8))
for i in range(shape[0]):
    for j in range(shape[1]):
        res = lnprob(sample0.chain[0][i, j], returninfo=True)
        parfit[i, j] = [res.plx_best, res.pmra_best, res.pmdec_best,
                        res.chisq_sep, res.chisq_PA,
                        res.chisq_H, res.chisq_HG, res.chisq_G]

out = fits.HDUList(fits.PrimaryHDU(sample0.chain[0].astype(np.float32)))
out.append(fits.PrimaryHDU(sample0.lnprobability[0].astype(np.float32)))
out.append(fits.PrimaryHDU(parfit.astype(np.float32)))
for i in range(1000):
    filename = os.path.join(args.output_dir, 'HIP%d_chain%03d.fits' % (HipID, i))
    if not os.path.isfile(filename):
        print('Writing output to {0}'.format(filename))
        out.writeto(filename, overwrite=False)
        exit()
