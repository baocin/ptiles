#!/bin/bash
# Run generate_state_v8.py for CONUS states (skip AK/HI/PR/VI which have huge marine bboxes)
# States to skip: AK (dateline bbox issue), HI, PR (Puerto Rico), VI (US Virgin Islands)
LOG="/home/aoi/kino/projects/ptiles/logs/build_conus_v8.log"
SCRIPTS="/home/aoi/kino/projects/ptiles/scripts"
DATA="/home/aoi/kino/projects/ptiles/data/states"

cd /home/aoi/kino/projects/ptiles

SKIP="AK HI PR VI"
STATES="AL AZ AR CA CO CT DE DC FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"

for st in $STATES; do
    echo "$(date) === Starting $st ===" | tee -a $LOG
    uv run --with h3 --with mapbox-vector-tile --with zstandard \
        python scripts/generate_state_v8.py $st >> $LOG 2>&1
    rc=$?
    echo "$(date) === $st exit code: $rc ===" | tee -a $LOG
done

echo "$(date) === CONUS buildings complete ===" | tee -a $LOG
echo "Files produced:" | tee -a $LOG
ls -lh $DATA/*.buildings_v8.ptiles 2>/dev/null | tee -a $LOG
