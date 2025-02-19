import os

from cffi import FFI
from common.ffi_wrapper import suffix

mpc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))

def _get_libmpc(mpc_id):
    libmpc_fn = os.path.join(mpc_dir, "libmpc%d%s" % (mpc_id, suffix()))

    ffi = FFI()
    ffi.cdef("""
    typedef struct {
    double x_ego, v_ego, a_ego, x_l, v_l, a_l;
    } state_t;


    typedef struct {
    double x_ego[21];
    double v_ego[21];
    double a_ego[21];
    double j_ego[20];
    double x_l[21];
    double v_l[21];
    double a_l[21];
    double t[21];
    double cost;
    } log_t;

    void init(double ttcCost, double distanceCost, double accelerationCost, double jerkCost);
    void init_with_simulation(double v_ego, double x_l, double v_l, double a_l, double l);
    int run_mpc(state_t * x0, log_t * solution,
                double l, double a_l_0, double TR);
    """)

    return (ffi, ffi.dlopen(libmpc_fn))

mpcs = [_get_libmpc(i) for i in range(6)]

def get_libmpc(mpc_id):
    return mpcs[mpc_id]
