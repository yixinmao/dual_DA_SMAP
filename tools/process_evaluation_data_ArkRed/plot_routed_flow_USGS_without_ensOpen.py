
import numpy as np
import sys
import datetime as dt
import pandas as pd
import os
import xarray as xr
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from bokeh.plotting import figure, output_file, save
from bokeh.io import reset_output
import bokeh
import properscoring as ps

from tonic.io import read_config, read_configobj
from plot_utils import read_USGS_data, read_RVIC_output, kge, nse, rmse, nensk, crps


# ===================================================== #
# Read cfg
# ===================================================== #
cfg = read_configobj(sys.argv[1])


# ===================================================== #
# Parameters
# ===================================================== #
# --- USGS data --- #
# Site info file; needs to have columns "short_name" and "site_no"
site_info_csv = cfg['USGS']['site_info_csv']
# Directory of USGS data
usgs_data_dir = cfg['USGS']['usgs_data_dir']

# --- Routed --- #
# Openloop nc
openloop_nc = cfg['ROUTE']['openloop_nc']

# Ensemble size
N = cfg['ROUTE']['N']
# Ensemble nc; "{}" will be replaced by ensemble index
ensemble_basenc = cfg['ROUTE']['ensemble_basenc']

# Time lag of routed data with local time [hours];
# Example: if local time is UTC-6 (ArkRed) and routed data is in UTC, then time_lag = 6
time_lag = cfg['ROUTE']['time_lag']

# --- Time --- #
start_time = pd.to_datetime(cfg['ROUTE']['start_time'])
end_time = pd.to_datetime(cfg['ROUTE']['end_time'])
start_year = start_time.year
end_year = end_time.year

# --- Domain file ("mask" and "area" will be used) --- #
domain_nc = cfg['DOMAIN']['domain_nc']

# --- RVIC output param remap nc files --- #
# "{}" will be replaced by site name
rvic_subbasin_nc = cfg['DOMAIN']['rvic_subbasin_nc']
    
# --- VIC forcing file basedir ("YYYY.nc" will be appended) --- #
force_basedir = cfg['BASEFLOW_FRAC']['force_basedir']

# --- VIC opnloop output history nc (this is to calculate baseflow fraction) --- # 
vic_openloop_hist_nc = cfg['BASEFLOW_FRAC']['vic_openloop_hist_nc']

# --- Output --- #
output_dir = cfg['OUTPUT']['output_dir']


# ===================================================== #
# Load USGS data
# ===================================================== #
# --- Load site info --- #
df_site_info = pd.read_csv(site_info_csv, dtype={'site_no': str})
dict_sites = {}  # {site: site_no}
for i in df_site_info.index:
    site = df_site_info.loc[i, 'short_name']
    site_no = df_site_info.loc[i, 'site_no']
    dict_sites[site] = site_no
    
# --- Load USGS streamflow data --- #
dict_flow_usgs = {}  # {site: ts}
for site in dict_sites.keys():
    print('Loading USGS data for site {}...'.format(site))
    site_no = dict_sites[site]
    filename = os.path.join(usgs_data_dir, '{}.txt'.format(site_no))
    ts_flow = read_USGS_data(filename, columns=[1], names=['flow'])['flow']
    dict_flow_usgs[site] = ts_flow.truncate(before=start_time, after=end_time)

# --- Get USGS drainage area (mi2) --- #
dict_usgs_drainage_area = {}  # {site: area}
for i in df_site_info.index:
    site = df_site_info.loc[i, 'short_name']
    drainage_area = df_site_info.loc[i, 'drain_area_va']
    dict_usgs_drainage_area[site] = drainage_area * 1.60934 * 1.60934  # convert [mi2] to [km2]


# ===================================================== #
# Load and process routed data
# ===================================================== #
# --- Load openloop data --- #
df_openloop, dict_outlet = read_RVIC_output(openloop_nc)

# --- Load ensemble data --- #
list_da_ensemble = []
for i in range(N):
    filename = ensemble_basenc.format(i+1)
    df, dict_outlet = read_RVIC_output(filename)
    da = xr.DataArray(df, dims=['time', 'site'])
    list_da_ensemble.append(da)
# Concat all ensemble members
da_ensemble = xr.concat(list_da_ensemble, dim='N')

# --- Shift all routed data data to local time --- #
df_openloop.index = df_openloop.index - pd.DateOffset(hours=time_lag)
da_ensemble['time'] = pd.to_datetime(da_ensemble['time'].values) - pd.DateOffset(hours=time_lag)

# --- Average all routed data to daily (of local time) --- #
df_openloop_daily = df_openloop.resample('1D', how='mean')
da_ensemble_daily = da_ensemble.resample('1D', dim='time', how='mean')

# --- Calculate ensemble mean --- #
da_ensMean_daily = da_ensemble_daily.mean(dim='N')


# ===================================================== #
# Load basin information
# ===================================================== #
ds_domain = xr.open_dataset(domain_nc)
da_area = ds_domain['area']
da_domain = ds_domain['mask']

# --- Basin domain --- #
dict_da_frac = {}
for site in dict_sites.keys():
    da_frac = xr.open_dataset(rvic_subbasin_nc.format(site))['fraction']
    dict_da_frac[site] = da_frac

# --- Basin area --- #
dict_basin_area = {}
for site in dict_sites.keys():
    basin_area = float(da_area.where(dict_da_frac[site]>0).sum())  # [m2]
    basin_area = basin_area / 1000 / 1000  # convert to [km2]
    dict_basin_area[site] = basin_area
    print(site, basin_area)


# ===================================================== #
# Calculate basin monthly runoff ratio
# ===================================================== #
# --- Load precipitation data --- #
list_da_prec = []
for year in range(start_year, end_year+1):
    da = xr.open_dataset(
        '{}{}.nc'.format(force_basedir, year))['PREC']
    list_da_prec.append(da)
da_prec = xr.concat(list_da_prec, dim='time').sel(time=slice(start_time, end_time))
da_prec_mon = da_prec.resample('1M', dim='time', how='sum')  # [mm/month]

dict_runoff_ratio_openloop_routed_mon = {}
dict_runoff_ratio_usgs_mon = {}
for site in dict_sites.keys():
    # Precipitation sum
    da_basin_prec_mon = da_prec_mon.where(dict_da_frac[site]>0)
    ts_basin_prec_mon = (da_basin_prec_mon * da_area).sum(
        dim='lat').sum(dim='lon').to_series()  # [mm*m2/mon]
    ts_basin_prec_mon = ts_basin_prec_mon * 1000 * 1000 \
        / np.power(25.4*12, 3) / (365/12 * 86400)  # converted to [cfs]
    # Streamflow
    ts_openloop_routed_mon = df_openloop_daily.loc[:, site].resample('1M', how='mean')  # [cfs]
    ts_usgs_mon = dict_flow_usgs[site].resample('1M', how='mean')  # [cfs]
    # Monthly runoff ratio
    dict_runoff_ratio_openloop_routed_mon[site] = ts_openloop_routed_mon / ts_basin_prec_mon
    dict_runoff_ratio_usgs_mon[site] = ts_usgs_mon / ts_basin_prec_mon


# ===================================================== #
# Calculate basin baseflow fraction (average baseflow / total runoff)
# ===================================================== #
# --- Load openloop VIC runoff --- #
ds_openloop = xr.open_dataset(vic_openloop_hist_nc)
da_totrunoff = ds_openloop['OUT_RUNOFF'] + ds_openloop['OUT_BASEFLOW']
da_baseflow = ds_openloop['OUT_BASEFLOW']

# --- Calculate basin-avg baseflow fraction --- #
dict_baseflow_fraction = {}
for site in dict_sites.keys():
    # Baseflow sum
    da_basin_baseflow = da_baseflow.where(dict_da_frac[site]>0)
    basin_baseflow = (da_basin_baseflow * da_area).sum(
        dim='lat').sum(dim='lon').mean(dim='time').values  # [mm*m2/step]
    # Total runoff sum
    da_basin_totrunoff = da_totrunoff.where(dict_da_frac[site]>0)
    basin_totrunoff = (da_basin_totrunoff * da_area).sum(
        dim='lat').sum(dim='lon').mean(dim='time').values  # [mm*m2/step]
    # Baseflow fraction
    baseflow_fraction = basin_baseflow / basin_totrunoff
    dict_baseflow_fraction[site] = baseflow_fraction


# ===================================================== #
# Plot
# ===================================================== #
for site in dict_sites.keys():
    # --- Some calculation --- #
    ts_usgs = dict_flow_usgs[site]
    ts_openloop = df_openloop_daily.loc[:, site]
    ts_usgs = ts_usgs[ts_openloop.index].dropna()
    ts_openloop = ts_openloop[ts_usgs.index].dropna()
    ts_ensMean = da_ensMean_daily.sel(site=site).to_series()[ts_usgs.index].dropna()
    ensemble_daily = da_ensemble_daily.sel(
        site=site, time=ts_usgs.index).transpose('time', 'N').values
    # KGE
    kge_openloop = kge(ts_openloop, ts_usgs)
    kge_ensMean = kge(ts_ensMean, ts_usgs)
    # NSE
    nse_openloop = nse(ts_openloop, ts_usgs)
    nse_ensMean = nse(ts_ensMean, ts_usgs)
    # RMSE
    rmse_openloop = rmse(ts_openloop, ts_usgs)
    rmse_ensMean = rmse(ts_ensMean, ts_usgs)
    # logRMSE
    rmseLog_openloop = rmse(np.log(ts_openloop+1), np.log(ts_usgs+1))
    rmseLog_ensMean = rmse(np.log(ts_ensMean+1), np.log(ts_usgs+1))
    # CRPS
    crps_ens = crps(ts_usgs, ensemble_daily)
    # Normalized ensemble skill (NENSK)
    nensk_ens = nensk(ts_usgs, ensemble_daily)
    # --- Plot regular plot --- #
    fig = plt.figure(figsize=(20, 6))
    # Ensemble
    for i in range(N):
        label = 'Ensemble updated (mean={:.1f})\n' \
                'KGE={:.2f} NSE={:.2f} RMSE={:.1f} logRMSE={:.2f}\n' \
                'CRPS={:.1f} NENSK={:.1f}'.format(
                    ts_ensMean.mean(),
                    kge_ensMean, nse_ensMean, rmse_ensMean, rmseLog_ensMean,
                    crps_ens, nensk_ens) \
            if i == 0 else '_nolegend_'
        ts = da_ensemble_daily.sel(N=i, site=site).to_series() / 1000
        ts.plot(color='blue', alpha=0.2, label=label)
    (ts_ensMean/1000).plot(color='blue', linewidth=2)
    # USGS
    (ts_usgs / 1000).plot(color='black', label='USGS (mean={:.1f})'.format(
        ts_usgs.mean()))
    # Openloop
    (ts_openloop / 1000).plot(
        color='magenta', style='--',
        label='Open-loop (mean={:.1f})\n' \
              'KGE={:.2f} NSE={:.2f} RMSE={:.1f} logRMSE={:.2f}'.format(
            ts_openloop.mean(), kge_openloop, nse_openloop, rmse_openloop, rmseLog_openloop))
    # Make plot better
    plt.ylabel('Streamflow (thousand cfs)', fontsize=16)
    plt.legend(fontsize=16)
    plt.title("{}\nUSGS drainage = {:.0f} km2, VIC area = {:.0f} km2\n" \
              "VIC baseflow fraction = {:.2f}".format(
                  site, dict_usgs_drainage_area[site], dict_basin_area[site],
                  dict_baseflow_fraction[site]),
              fontsize=16)
    # Save to file
    fig.savefig(os.path.join(output_dir, 'flow_daily.{}.png'.format(site)),
                format='png', bbox_inches='tight', pad_inches=0)
    
    # --- Interactive --- #
    output_file(os.path.join(output_dir, 'flow_daily.{}.html'.format(site)))
    p = figure(title='Streamflow at {}\nUSGS drainage = {:.0f} km2, VIC area = {:.0f} km2'.format(
                    site, dict_usgs_drainage_area[site], dict_basin_area[site]),
               x_axis_label="Time", y_axis_label="Streamflow (thousand csv)",
               x_axis_type='datetime', width=1000, height=500)
    # plot ensemble
    for i in range(N):
        label = 'Ensemble updated (mean={:.1f})\n' \
                'KGE={:.2f} NSE={:.2f} RMSE={:.1f} logRMSE={:.2f}\n' \
                'CRPS={:.1f} NENSK={:.1f}'.format(
                    ts_ensMean.mean(),
                    kge_ensMean, nse_ensMean, rmse_ensMean, rmseLog_ensMean,
                    crps_ens, nensk_ens) \
            if i == 0 else '_nolegend_'
        ts = da_ensemble_daily.sel(N=i, site=site).to_series() / 1000
        p.line(ts.index, ts.values, color="blue", line_dash="solid",
               alpha=0.2, line_width=2, legend=label)
    p.line(ts_ensMean.index, (ts_ensMean/1000).values, color="blue", line_dash="solid",
           line_width=2)
    # plot USGS
    p.line(ts_usgs.index, (ts_usgs/1000).values, color="black", line_dash="solid",
           legend="USGS (mean={:.2f})".format(ts_usgs.mean()), line_width=2)
    # plot open-loop
    p.line(ts_openloop.index, (ts_openloop/1000).values, color="magenta", line_dash="dashed",
           legend='Open-loop (mean={:.1f})\n' \
              'KGE={:.2f} NSE={:.2f} RMSE={:.1f} logRMSE={:.2f}'.format(
                  ts_openloop.mean(), kge_openloop, nse_openloop,
                  rmse_openloop, rmseLog_openloop),
           line_width=2)
    # Save
    save(p)
    

# ===================================================== #
# Zoom in
# ===================================================== #
for site in dict_sites.keys():
    # --- Some calculation --- #
    ts_usgs = dict_flow_usgs[site]
    ts_openloop = df_openloop_daily.loc[:, site]
    ts_usgs = ts_usgs[ts_openloop.index].dropna()
    ts_openloop = ts_openloop[ts_usgs.index].dropna()
    ts_ensMean = da_ensMean_daily.sel(site=site).to_series()[ts_usgs.index].dropna()
    ensemble_daily = da_ensemble_daily.sel(
        site=site, time=ts_usgs.index).transpose('time', 'N').values
    # KGE
    kge_openloop = kge(ts_openloop, ts_usgs)
    kge_ensMean = kge(ts_ensMean, ts_usgs)
    # NSE
    nse_openloop = nse(ts_openloop, ts_usgs)
    nse_ensMean = nse(ts_ensMean, ts_usgs)
    # RMSE
    rmse_openloop = rmse(ts_openloop, ts_usgs)
    rmse_ensMean = rmse(ts_ensMean, ts_usgs)
    # logRMSE
    rmseLog_openloop = rmse(np.log(ts_openloop+1), np.log(ts_usgs+1))
    rmseLog_ensMean = rmse(np.log(ts_ensMean+1), np.log(ts_usgs+1))
    # CRPS
    crps_ens = crps(ts_usgs, ensemble_daily)
    # Normalized ensemble skill (NENSK)
    nensk_ens = nensk(ts_usgs, ensemble_daily)
    # --- Plot regular plot --- #
    fig = plt.figure(figsize=(20, 6))
    ax = plt.axes()
    # Ensemble
    for i in range(N):
        label = 'Ensemble updated (mean={:.1f})\n' \
                'KGE={:.2f} NSE={:.2f} RMSE={:.1f} logRMSE={:.2f}\n' \
                'CRPS={:.1f} NENSK={:.1f}'.format(
                    ts_ensMean.mean(),
                    kge_ensMean, nse_ensMean, rmse_ensMean, rmseLog_ensMean,
                    crps_ens, nensk_ens) \
            if i == 0 else '_nolegend_'
        ts = da_ensemble_daily.sel(N=i, site=site).to_series() / 1000
        ts.plot(color='blue', alpha=0.2, label=label)
    (ts_ensMean/1000).plot(color='blue', linewidth=2)
    # USGS
    (ts_usgs / 1000).plot(color='black', label='USGS (mean={:.1f})'.format(
        ts_usgs.mean()))
    # Openloop
    (ts_openloop / 1000).plot(
        color='magenta', style='--',
        label='Open-loop (mean={:.1f})\n' \
              'KGE={:.2f} NSE={:.2f} RMSE={:.1f} logRMSE={:.2f}'.format(
            ts_openloop.mean(), kge_openloop, nse_openloop, rmse_openloop, rmseLog_openloop))
    # Make plot better
    plt.ylabel('Streamflow (thousand cfs)', fontsize=16)
    plt.legend(fontsize=16)
    plt.title("{}\nUSGS drainage = {:.0f} km2, VIC area = {:.0f} km2\n" \
              "VIC baseflow fraction = {:.2f}".format(
                  site, dict_usgs_drainage_area[site], dict_basin_area[site],
                  dict_baseflow_fraction[site]),
              fontsize=16)
    plt.xlim(['2017-03-01', '2017-06-30'])
#     if site == 'arkansas':
#         plt.ylim([-0.3, 8])
#     elif site == 'deep':
#         plt.ylim([-2, 100])
    for t in ax.get_xticklabels():
        t.set_fontsize(20)
    for t in ax.get_yticklabels():
        t.set_fontsize(20)
    # Save to file
    fig.savefig(os.path.join(output_dir, 'flow_daily.zoomin.{}.png'.format(site)),
                format='png', bbox_inches='tight', pad_inches=0)




