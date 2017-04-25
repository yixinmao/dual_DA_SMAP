# =========================================================== #
# This script produces true and synthetic surface soil moisture measurements
#    - Run VIC with "truth" forcings and perturbed states --> "truth"
#    - Add random noise to "truth" top-layer soil moisture --> synthetic measurements
# =========================================================== #

import sys
import os
import numpy as np
import xarray as xr
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import shutil
from collections import OrderedDict

from tonic.models.vic.vic import VIC
from tonic.io import read_configobj

from da_utils import (Forcings, setup_output_dirs, propagate,
                      calculate_sm_noise_to_add_magnitude,
                      perturb_soil_moisture_states,
                      calculate_max_soil_moist_domain,
                      convert_max_moist_n_state, VarToPerturb,
                      find_global_param_value, propagate_linear_model,
                      concat_clean_up_history_file,
                      calculate_scale_n_whole_field,
                      calculate_cholesky_L,
                      save_updated_states,
                      run_vic_assigned_states)

# =========================================================== #
# Load command line arguments
# =========================================================== #
cfg = read_configobj(sys.argv[1])
mpi_proc = int(sys.argv[2])


# =========================================================== #
# Set random generation seed
# =========================================================== #
np.random.seed(cfg['CONTROL']['seed'])


# =========================================================== #
# Process some config parameters
# =========================================================== #
print('Processing config parameters...')
# Simulation time
start_time = pd.to_datetime(cfg['TIME_INDEX']['start_time'])
end_time = pd.to_datetime(cfg['TIME_INDEX']['end_time'])
start_year = start_time.year
end_year = end_time.year

# Identify output sub-directories
dirs = setup_output_dirs(os.path.join(cfg['CONTROL']['root_dir'],
                                      cfg['OUTPUT']['output_basedir']),
                         mkdirs=['truth', 'synthetic_meas', 'plots'])
truth_subdirs = setup_output_dirs(dirs['truth'],
                                  mkdirs=['global', 'history', 'states',
                                          'logs'])
# VIC global template file
global_template = os.path.join(cfg['CONTROL']['root_dir'],
                               cfg['VIC']['vic_global_template'])


# =========================================================== #
# Load data
# =========================================================== #
# --- Load measurement data (to get time points) --- #
print('Loading orig. synthetic measurement data...')
ds_meas_orig = xr.open_dataset(os.path.join(
    dirs['synthetic_meas'],
    'synthetic_meas.{}_{}.nc'.format(start_time.strftime('%Y%m%d'),
                                     end_time.strftime('%Y%m%d'))))
da_meas_orig = ds_meas_orig['simulated_surface_sm']
# Only select out the period within the EnKF run period
da_meas = da_meas_orig.sel(time=slice(start_time, end_time))
# Convert da_meas dimension to [time, lat, lon, m] (currently m = 1)
time = da_meas['time']
lat = da_meas['lat']
lon = da_meas['lon']
data = da_meas.values.reshape((len(time), len(lat), len(lon), 1))
da_meas = xr.DataArray(data, coords=[time, lat, lon, [0]],
                       dims=['time', 'lat', 'lon', 'm'])
# --- Load "truth" states --- #
print('Loading orig. truth states data...')
dict_truth_state_nc = OrderedDict()
list_ds_truth_state = []
list_da_truth_sm = []
dict_rescaled_state_nc = OrderedDict()
for t, time in enumerate(pd.to_datetime(da_meas['time'].values)):
    state_time = pd.to_datetime(time)
    truth_state_nc = os.path.join(
        truth_subdirs['states'],
        'perturbed.state.{}_{:05d}.nc'.format(
                time.strftime('%Y%m%d'),
                time.hour*3600+time.second))
    dict_truth_state_nc[state_time] = truth_state_nc
    ds = xr.open_dataset(truth_state_nc)
    list_ds_truth_state.append(ds)
    list_da_truth_sm.append(ds['STATE_SOIL_MOISTURE'])
    rescaled_state_nc = os.path.join(
        truth_subdirs['states'],
        'rescaled.state.{}_{:05d}.nc'.format(
                time.strftime('%Y%m%d'),
                time.hour*3600+time.second))
    dict_rescaled_state_nc[state_time] = rescaled_state_nc

# --- Load "openloop" history file --- #
ds_openloop_hist = xr.open_dataset(os.path.join(
        cfg['CONTROL']['root_dir'],
        cfg['OPENLOOP']['openloop_hist']))

# =========================================================== #
# Rescale "truth" soil moisture states
# =========================================================== #
print('Rescaling \"truth\" soil moisture states...')

# --- Concat "truth" soil moisture states --- #
print('\tConcatenating truth...')
da_truth_sm_concat = xr.concat(list_da_truth_sm, dim='time')
da_truth_sm_concat['time'] = da_meas['time']  # [time, veg, snow, nlayer, lat, lon]

# --- Extract openloop soil moisture states at the same time points as measurements --- #
# --- NOTE: history file results are timestep-beginning!! --- #
print('\tExtracting openloop soil moistures...')
da_openloop_sm = ds_openloop_hist['OUT_SOIL_MOIST']
# Adjust history time to be timestep-end
times = pd.to_datetime(da_openloop_sm['time'].values)
times = times - pd.DateOffset(hours=24/cfg['VIC']['model_steps_per_day'])
da_openloop_sm['time'] = times
# Select out measurement time points
da_openloop_sm = da_openloop_sm.sel(time=da_meas['time'])  # [time, nlayer, lat, lon]

# --- Rescale "truth" soil moisture states --- #
print('\tRescaling for each grid-cell mean value...')
# Calculate openloop mean and variance for each layer, lat and lon
da_openloop_sm_mean = da_openloop_sm.mean(dim='time')  # [nlayer, lat, lon]
da_openloop_sm_std = da_openloop_sm.std(dim='time')  # [nlayer, lat, lon]
### Rescale truth soil moisture states ###
lat_coord = da_openloop_sm['lat']
lon_coord = da_openloop_sm['lon']
nlayer_coord = da_openloop_sm['nlayer']
time_coord = da_openloop_sm['time']
veg_coord = da_truth_sm_concat['veg_class']
snow_coord = da_truth_sm_concat['snow_band']
# Determine the total number of loops
nloop = len(lat_coord) * len(lon_coord) * len(nlayer_coord)
# Convert variables to np.array and straighten into nloop
openloop_sm_mean = da_openloop_sm_mean.values.reshape([nloop])  # [nloop]
openloop_sm_std = da_openloop_sm_std.values.reshape([nloop])  # [nloop]
truth_sm = da_truth_sm_concat.values.reshape(
    [len(time_coord), len(veg_coord),
     len(snow_coord), nloop])  # [time, veg, snow, nloop]
truth_sm_cell = np.nanmean(np.nanmean(truth_sm, axis=1), axis=1)  # [time, nloop]
# Calculate cell-mean truth statistics
truth_sm_cell_mean = np.nanmean(truth_sm_cell, axis=0)  # [nloop]
truth_sm_cell_std = np.nanstd(truth_sm_cell, axis=0)  # [nloop]
# Calculate rescaled truth soil moisture states for grid-cell-mean values
# --- sm_rescaled_cell = (sm_truth - mean_truth) * (std_open / std_truth) + mean_open --- #
print('Rescaling for each grid cell...')
sm_rescaled_cell = np.array(list(map(
    lambda i: (truth_sm_cell[:, i] - truth_sm_cell_mean[i]) * \
              (openloop_sm_std[i] / truth_sm_cell_std[i]) + openloop_sm_mean[i],
    range(nloop))))  # [nloop, time]
# --- Rescale for each tile within each layer and grid cell --- #
# sm_rescaled_tile = (sm_tile - sm_tile_mean) + sm_rescale_cell
print('Rescaling for each tile...')
nloop2 = nloop * len(time_coord)
sm_rescaled_cell = sm_rescaled_cell.reshape([nloop2])  # [nloop2]
truth_sm = np.rollaxis(truth_sm, 3, 0)  # [nloop, time, veg, snow]
truth_sm = truth_sm.reshape([nloop2, len(veg_coord), len(snow_coord)])  # [nloop2, veg, snow]
truth_sm_mean = np.nanmean(np.nanmean(truth_sm, axis=1), axis=1)  # [nloop2]
sm_rescaled = np.array(list(map(
    lambda i: (truth_sm[i, :, :] - truth_sm_mean[i]) + sm_rescaled_cell[i],
    range(nloop2))))  # [nloop2, veg, snow]
# --- Convert back to original state shape --- #
sm_rescaled = sm_rescaled.reshape(
    [len(nlayer_coord), len(lat_coord), len(lon_coord), len(time_coord),
    len(veg_coord), len(snow_coord)])  # [nlayer, lat, lon, time, veg, snow]
sm_rescaled = np.rollaxis(sm_rescaled, 3, 0)  # [time, nlayer, lat, lon, veg, snow]
sm_rescaled = np.rollaxis(sm_rescaled, 2, 6)  # [time, nlayer, lon, veg, snow, lat]
sm_rescaled = np.rollaxis(sm_rescaled, 2, 6)  # [time, nlayer, veg, snow, lat, lon]
sm_rescaled = np.rollaxis(sm_rescaled, 1, 4)  # [time, veg, snow, nlayer, lat, lon]
# Put into da
da_sm_rescaled = xr.DataArray(
    sm_rescaled,
    coords=[time_coord, veg_coord, snow_coord, nlayer_coord, lat_coord, lon_coord],
    dims=['time', 'veg_class', 'snow_class', 'nlayer', 'lat', 'lon'])

# --- Reset negative and above-maximum soil moistures --- #
global_template = os.path.join(
                    cfg['CONTROL']['root_dir'],
                    cfg['VIC']['vic_global_template'])
da_max_moist = calculate_max_soil_moist_domain(global_template)  # [nlayer, lat, lon]
# Reset negative to zero
sm = da_sm_rescaled.values
sm[sm<0] = 0
da_sm_rescaled[:] = sm
# Reset above-maximum to maximum
sm = da_sm_rescaled.values  # [time, veg, snow, nlayer, lat, lon]
max_moist = da_max_moist.values  # [nlayer, lat, lon]
max_moist_expanded = np.zeros([len(time_coord), len(veg_coord), len(snow_coord),
                               len(nlayer_coord), len(lat_coord), len(lon_coord)])
max_moist_expanded[:] = max_moist
sm[sm>max_moist_expanded] = max_moist_expanded[sm>max_moist_expanded]
da_sm_rescaled[:] = sm

# --- Save rescaled states to file --- #
print('Save rescaled states...')

for state_time, state_nc in dict_truth_state_nc.items():
    save_updated_states(
        state_nc_before_update=state_nc,
        da_sm_updated=da_sm_rescaled.sel(time=state_time),
        out_vic_state_nc=dict_rescaled_state_nc[state_time])


# =========================================================== #
# Run VIC from rescaled states...
# =========================================================== #
print('Running VIC with "true" forcings and rescaled states...')
# --- Create class VIC --- #
vic_exe = VIC(os.path.join(cfg['CONTROL']['root_dir'], cfg['VIC']['exe']))
# --- Prepare some variables --- #
# initial state nc
init_state_nc = os.path.join(
    cfg['CONTROL']['root_dir'],
    cfg['VIC']['vic_initial_state'])
# other variables
global_template = os.path.join(
                    cfg['CONTROL']['root_dir'],
                    cfg['VIC']['vic_global_template'])
vic_forcing_basepath = os.path.join(
        cfg['CONTROL']['root_dir'],
        cfg['VIC']['truth_forcing_nc_basepath'])
# --- run VIC with assigned states --- #
run_vic_assigned_states(
    start_time=start_time,
    end_time=end_time,
    vic_exe=vic_exe,
    init_state_nc=init_state_nc,
    dict_assigned_state_nc=dict_rescaled_state_nc,
    global_template=global_template,
    vic_forcing_basepath=vic_forcing_basepath,
    vic_model_steps_per_day=cfg['VIC']['model_steps_per_day'],
    output_global_root_dir=truth_subdirs['global'],
    output_vic_history_root_dir=truth_subdirs['history'],
    output_vic_log_root_dir=truth_subdirs['logs'],
    mpi_proc=mpi_proc,
    mpi_exe=cfg['VIC']['mpi_exe'])
# --- Concatenate all history files --- #
hist_concat_nc = os.path.join(truth_subdirs['history'],
                              'history.concat.rescaled.{}_{:05d}-{}_{:05d}.nc'.format(
                                        start_time.strftime('%Y%m%d'),
                                        start_time.hour*3600+start_time.second,
                                        end_time.strftime('%Y%m%d'),
                                        end_time.hour*3600+end_time.second))
list_history_files = []
for year in range(start_year, end_year+1):
    list_history_files.append(os.path.join(
        truth_subdirs['history'],
        'history.concat.{}.nc'.format(year)))
concat_clean_up_history_file(list_history_files,
                             hist_concat_nc)

# =========================================================== #
# Simulate synthetic measurement - Extract top-layer soil
# moisture from "truth" at the end of each day, and add noise
# =========================================================== #

print('Simulating synthetic measurements...')

# --- Load history file --- #
ds_hist = xr.open_dataset(hist_concat_nc)

# --- Select out times of measurement --- #
ds_hist_meas_times = ds_hist.sel(time=da_meas['time'])

# --- Select top-layer soil moisture --- #
da_sm1_true = ds_hist_meas_times['OUT_SOIL_MOIST'].sel(nlayer=0)

# --- Add noise --- #
# Generate the standard deviation of noise to be added for each grid cell
da_sigma = da_sm1_true[0, :, :].copy(deep=True)
da_sigma[:] = cfg['SYNTHETIC_MEAS']['sigma']
# Add noise
VarToPerturb_sm1 = VarToPerturb(da_sm1_true) # create class
da_sm1_perturbed = VarToPerturb_sm1.add_gaussian_white_noise(
                        da_sigma, da_max_moist.sel(nlayer=0),
                        adjust_negative=True)

# --- Save synthetic measurement to netCDF file --- #
ds_simulated = xr.Dataset({'simulated_surface_sm': da_sm1_perturbed})
ds_simulated.to_netcdf(
    os.path.join(
        dirs['synthetic_meas'],
        'synthetic_meas.rescaled_v1.{}_{}.nc'.format(
            start_time.strftime('%Y%m%d'),
            end_time.strftime('%Y%m%d'))),
    format='NETCDF4_CLASSIC')
