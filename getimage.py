import ee
import geemap
import os

# 1. Authenticate and Initialize Earth Engine
# (The first time you run this, it will open a browser window to grant project access)
ee.Authenticate()
ee.Initialize(project='aidenglacierviewer')

# 2. Define your target area (Bounding Box around Mt. Rainier, WA)
# Coordinates: [Min Longitude, Min Latitude, Max Longitude, Max Latitude]
rainier_aoi = ee.Geometry.BBox(-121.85, 46.80, -121.65, 46.92)

# 3. Define the years you want to extract
start_year = 1980
end_year = 2024

# Create an output directory for the downloaded images
output_dir = "./rainier_yearly_images"
if not os.path.exists(output_dir):
    os.makedirs(output_dir)

print("Starting yearly imagery extraction...")

# 4. Loop through each year to build and download composites
for year in range(start_year, end_year + 1):
    start_date = f"{year}-07-15"
    end_date = f"{year}-09-15"
    
    # Pull Landsat 8 Surface Reflectance Tier 1 data
    annual_collection = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
                         .filterBounds(rainier_aoi)
                         .filterDate(start_date, end_date)
                         .filter(ee.Filter.lt('CLOUD_COVER', 15))) # Exclude heavy cloud days
    
    # Take the median pixel value to strip away temporary clouds and shadows
    # Then clip it strictly to our Mt. Rainier bounding box
    yearly_composite = annual_collection.median().clip(rainier_aoi)
    
    # Select the standard True Color bands: Red (B4), Green (B3), Blue (B2)
    # Landsat 8 Surface Reflectance needs scaling, multiplying by 0.0000275 
    # brings it to typical visual ranges.
    visual_snapshot = yearly_composite.select(['SR_B4', 'SR_B3', 'SR_B2']).multiply(0.0000275)
    
    # 5. Export directly to your local drive using geemap
    file_name = os.path.join(output_dir, f"mt_rainier_{year}.tif")
    print(f"Downloading composite for year {year}...")
    
    try:
        geemap.download_ee_image(
            image=visual_snapshot,
            filename=file_name,
            region=rainier_aoi,
            scale=30, # Landsat native resolution is 30 meters per pixel
            crs='EPSG:4326' # Standard WGS 84 coordinate system
        )
    except Exception as e:
        print(f"Could not download data for {year}. (Likely insufficient cloud-free images): {e}")

print(f"Finished! Check your local folder: {os.path.abspath(output_dir)}")