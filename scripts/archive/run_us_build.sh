#!/bin/bash
# Run all three layers for every state sequentially
# Buildings (slowest, ~2 min avg), Roads (~2 min avg), Water (fast, ~10s)
set -euo pipefail

SCRIPTS="/home/aoi/kino/projects/ptiles/scripts"
PBF="/home/aoi/kino/projects/ptiles/data/pbfs"
OUT="/home/aoi/kino/projects/ptiles/data/states"
LOG="/home/aoi/kino/projects/ptiles/logs"

ST_ABBR=("AL" "AK" "AZ" "AR" "CA" "CO" "CT" "DE" "DC" "FL" "GA"
         "HI" "ID" "IL" "IN" "IA" "KS" "KY" "LA" "ME" "MD" "MA"
         "MI" "MN" "MS" "MO" "MT" "NE" "NV" "NH" "NJ" "NM" "NY"
         "NC" "ND" "OH" "OK" "OR" "PA" "RI" "SC" "SD" "TN" "TX"
         "UT" "VT" "VA" "WA" "WV" "WI" "WY")

ST_NAMES=("alabama" "alaska" "arizona" "arkansas" "california" "colorado"
          "connecticut" "delaware" "district-of-columbia" "florida" "georgia"
          "hawaii" "idaho" "illinois" "indiana" "iowa" "kansas" "kentucky"
          "louisiana" "maine" "maryland" "massachusetts" "michigan" "minnesota"
          "mississippi" "missouri" "montana" "nebraska" "nevada" "new-hampshire"
          "new-jersey" "new-mexico" "new-york" "north-carolina" "north-dakota"
          "ohio" "oklahoma" "oregon" "pennsylvania" "rhode-island"
          "south-carolina" "south-dakota" "tennessee" "texas" "utah" "vermont"
          "virginia" "washington" "west-virginia" "wisconsin" "wyoming")

echo "=== Starting US PTILES Build $(date) ===" > $LOG/us_build.log

for i in "${!ST_ABBR[@]}"; do
    abbr="${ST_ABBR[$i]}"
    name="${ST_NAMES[$i]}"
    pbf_file="$PBF/${name}-latest.osm.pbf"

    echo "$(date) === $abbr $name ===" | tee -a $LOG/us_build.log

    # Buildings
    bldg_out="$OUT/${abbr}.buildings_v8.ptiles"
    if [ ! -f "$bldg_out" ]; then
        echo "  Buildings..." | tee -a $LOG/us_build.log
        cd $SCRIPTS && uv run --with osmium --with h3 --with zstandard --with numpy --with shapely \
            python build_state_v8.py $abbr >> $LOG/us_build.log 2>&1
    else
        echo "  Buildings SKIP (exists)" | tee -a $LOG/us_build.log
    fi

    # Roads
    roads_out="$OUT/${abbr}.roads.ptiles"
    if [ ! -f "$roads_out" ]; then
        echo "  Roads..." | tee -a $LOG/us_build.log
        cd $SCRIPTS && uv run --with osmium --with h3 --with zstandard --with shapely \
            python build_roads.py "$pbf_file" "$roads_out" >> $LOG/us_build.log 2>&1
    else
        echo "  Roads SKIP (exists)" | tee -a $LOG/us_build.log
    fi

    # Water
    water_out="$OUT/${abbr}.water.ptiles"
    if [ ! -f "$water_out" ]; then
        echo "  Water..." | tee -a $LOG/us_build.log
        cd $SCRIPTS && uv run --with osmium --with h3 --with zstandard --with shapely \
            python build_water.py --source pbf --pbf "$pbf_file" --region "$name" --output "$water_out" >> $LOG/us_build.log 2>&1
    else
        echo "  Water SKIP (exists)" | tee -a $LOG/us_build.log
    fi
done

echo "$(date) === US PTILES Build Complete ===" | tee -a $LOG/us_build.log
echo "Buildings: $(ls $OUT/*.buildings_v8.ptiles 2>/dev/null | wc -l)" | tee -a $LOG/us_build.log
echo "Roads: $(ls $OUT/*.roads.ptiles 2>/dev/null | wc -l)" | tee -a $LOG/us_build.log
echo "Water: $(ls $OUT/*.water.ptiles 2>/dev/null | wc -l)" | tee -a $LOG/us_build.log
du -sh $OUT/ | tee -a $LOG/us_build.log
