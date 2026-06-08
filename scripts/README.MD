# Scripts for data rebuilding

## Build reference DB from Glims
build_ref_glacier_db.py

## Convert Glims shape data into GeoJson
glims_convert_GJ.py

## Filter GeoJSON data based on metadata property
filter_glaciers.py

## Split large geojson file into sharded and individual geojson file for 2D transition
split_glaciers.py
**TODO**: Instead of splitting into files, create a script to repopulate a Database for fast retrieval. 

## Get 3D image
getimage.py

## GeoJSON to MBTile conversion
tippecanoe -o output.mbtiles -Z 0 -z 14 --drop-densest-as-needed a.geojson