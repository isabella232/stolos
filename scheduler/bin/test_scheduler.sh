DIR="$( dirname "$( cd "$( dirname "$0" )" && pwd )")"

. $DIR/conf/scheduler-env.sh
echo $TASKS_JSON
echo $JOB_ID_DEFAULT_TEMPLATE
echo $JOB_ID_VALIDATIONS

nosetests \
`python -c '
from os.path import dirname ;
import scheduler ;
print(dirname(scheduler.__file__))'
`/tests $@
