#!/usr/bin/env bash
# Crop to HEIGHT, add a caption bar, strip metadata. Usage: finish_png.sh IN OUT HEIGHT "caption"
set -eu
magick "$1" -crop "1600x$3+0+0" +repage \
  \( -size 1552x -background '#0f1117' -fill '#9aa0ad' -font DejaVu-Sans -pointsize 19 -gravity West \
     -bordercolor '#0f1117' caption:"$4" -border 24x14 \) -append -depth 8 -strip "$2"
exiftool -all= -overwrite_original -q "$2"
