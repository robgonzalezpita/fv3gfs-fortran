#!/bin/bash

set -e
set -x

function  setExperimentNameToOriginal () {
    originalExperimentName=20160801.00Z.C48.32bit.non-mono
    sed -i.bak "1c\\
${originalExperimentName}
" $diagTable
}

modelType=$COMPILED_TAG_NAME

originalCheckSum=$(pwd)/tests/original_checksum_${modelType}.txt
experiment=new
rundir_host=$PWD/experiments/$experiment/rundir
rundir_container=/FV3/rundir
fixdatadir_host=$PWD/inputdata/fv3gfs-data-docker/fix.v201702
fixdatadir_container=/inputdata/fix.v201702
diagTable=$rundir_host/diag_table

# needed for makefile commands
export EXPERIMENT=$experiment

RUNDIR_CONTAINER=/FV3/rundir
RUNDIR_HOST=$(pwd)/experiments/$EXPERIMENT/rundir

bash create_case.sh -f $experiment
setExperimentNameToOriginal
docker run \
    --rm \
    -v $rundir_host:$rundir_container \
    -v $fixdatadir_host:$fixdatadir_container \
    -it $COMPILED_IMAGE

echo "Checking md5sum"
(
    cd experiments/$experiment/rundir
    md5sum -c $originalCheckSum
)
