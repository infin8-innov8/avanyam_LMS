# avanyam_terra runtime environment — source before any service command:
#   . ./conf/env.sh
export TERRA_ROOT="/home/rupesh/avanyam_lms/avanyam_terra"
export TERRA_OPT="$TERRA_ROOT/opt"
export TERRA_CONF="$TERRA_ROOT/conf"
export TERRA_DATA="$TERRA_ROOT/data"
export TERRA_LOGS="$TERRA_ROOT/logs"
export TERRA_RUN="$TERRA_ROOT/run"
export REDIS_DATA="$TERRA_DATA/redis"
export LD_LIBRARY_PATH="$TERRA_OPT/lib:${LD_LIBRARY_PATH:-}"
export PATH="$TERRA_OPT/bin:$PATH"
