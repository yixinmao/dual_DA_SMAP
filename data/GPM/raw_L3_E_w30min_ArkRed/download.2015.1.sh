#!/bin/bash

# This script donwloads IMERG L3 Early Run 30-min data for the Arkansas-Red domain

set -e

d=2015-01-01
while [ "$d" != 2015-04-01 ]; do 
    # Setup date
    echo $(date -d "$d" +%Y%m%d)
    doy=$(date -d "$d" +%j)  # day of year
    year=$(date -d "$d" +%Y)
    month=$(date -d "$d" +%m)
    day=$(date -d "$d" +%d)
    # Make new directory for this day
    output_dir=${year}/$month/$day/
    mkdir -p $output_dir
    # Loop over each 30-min
    for minday in {0000..1410..30}; do
        minday_num=`echo $minday | sed 's/^0*//'`  # remove the zeros
        hour=$((minday_num / 60))
        min=$((minday_num - hour * 60))
        min_end=$((min + 29))
        # add leadning zero to hour and min
        min=$(printf "%02d" $min)
        min_end=$(printf "%02d" $min_end)
        hour=$(printf "%02d" $hour)
        # Construct exact filename to download
        filename=https://gpm1.gesdisc.eosdis.nasa.gov/opendap/hyrax/GPM_L3/GPM_3IMERGHHE.05/$year/$doy/3B-HHR-E.MS.MRG.3IMERG.$(date -d "$d" +%Y%m%d)-S${hour}${min}00-E${hour}${min_end}59.$minday.V05B.HDF5.nc4?precipitationQualityIndex[730:1:900][1200:1:1300],precipitationUncal[730:1:900][1200:1:1300],lat[1200:1:1300],lon[730:1:900]
        # Download file
        wget $filename -O $output_dir/$(date -d "$d" +%Y%m%d).${hour}${min}.nc

    done

#    # Advance to the next day
    d=$(date -I -d "$d + 1 day")
done

