#!/usr/bin/env bash
# avanyam_aqua — HLS ladder transcode (task B7).
#
# Two renditions, 6-second fMP4 segments. Verified against a 20s source:
# 4 segments per rendition (6+6+6+2).
#
# The -g/-keyint_min/-sc_threshold/-force_key_frames group is NOT optional.
# ffmpeg's HLS muxer can only cut a segment on a keyframe. With x264's default
# GOP (~10s) a 6s -hls_time is silently ignored and you get 10s segments.
# That breaks the documented ladder, wrecks ABR switching, and makes the
# segment-count check in D3 pass or fail for the wrong reason.
set -euo pipefail

SRC="$1"; OUTDIR="$2"; FPS="${3:-25}"
GOP=$(( FPS * 6 ))

mkdir -p "$OUTDIR/v1080" "$OUTDIR/v480"

transcode() {  # W H VIDEO_BR AUDIO_BR DIR TAG
  local W=$1 H=$2 VB=$3 AB=$4 DIR=$5 TAG=$6
  local VKB=${VB%k} ABKB=${AB%k}
  "$(dirname "$0")/../opt/bin/ffmpeg" -hide_banner -loglevel error -y -i "$SRC" \
    -vf "scale=${W}:${H}" \
    -c:v libx264 -preset veryfast -b:v "$VB" -maxrate "$VB" -bufsize "$(( VKB * 2 ))k" \
    -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
    -force_key_frames "expr:gte(t,n_forced*6)" \
    -c:a aac -b:a "$AB" \
    -f hls -hls_time 6 -hls_playlist_type vod -hls_segment_type fmp4 \
    -hls_flags independent_segments \
    -hls_fmp4_init_filename "init_${TAG}.mp4" \
    -hls_segment_filename "$OUTDIR/${DIR}/seg_%03d.m4s" \
    "$OUTDIR/${DIR}/index.m3u8"
}

transcode 1920 1080 2500k 128k v1080 1080
transcode  854  480  900k  96k v480 480

BW1080=$(( (2500 + 128) * 1000 ))
BW480=$((  (900 +  96) * 1000 ))
cat > "$OUTDIR/master.m3u8" <<PLAYLIST
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=${BW1080},RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",NAME="1080p"
v1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=${BW480},RESOLUTION=854x480,CODECS="avc1.64001f,mp4a.40.2",NAME="480p"
v480/index.m3u8
PLAYLIST

echo "wrote $OUTDIR/master.m3u8"
