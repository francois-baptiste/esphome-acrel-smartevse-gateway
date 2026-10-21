"""Compile the real etek_dlb lambda text from config.yaml into a shared lib driven from Python.

The lambda body is extracted verbatim from the YAML (from the line containing the marker "ETEK_MIN_A = 6;"
to the end of its indented block), wrapped in a function, and compiled with g++ against stub sensors/globals
named like the ESPHome ids (`#define id(x) x`). Requires g++.
"""
import ctypes
import subprocess
import sys
import textwrap

PRELUDE = r'''
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>
#include <string>
#include <algorithm>
#define id(x) x
#define ESP_LOGI(tag, fmt, ...) do { if (g_verbose) { printf("[I] " fmt "\n", ##__VA_ARGS__); } } while (0)
#define ESP_LOGW(tag, fmt, ...) do { if (g_verbose) { printf("[W] " fmt "\n", ##__VA_ARGS__); } } while (0)
#define ESP_LOGD(tag, fmt, ...) do { if (g_verbose) { printf("[D] " fmt "\n", ##__VA_ARGS__); } } while (0)
static int g_verbose = 0;
static uint32_t g_now = 0;
static uint32_t millis() { return g_now; }
struct Write { int reg; double val; uint32_t t; };
static std::vector<Write> g_writes;
struct Call {
  int reg; double v = 0;
  Call &set_value(double x) { v = x; return *this; }
  void perform() { g_writes.push_back({reg, v, g_now}); }
};
struct Reg { int reg; float state = 0; Call make_call() { Call c; c.reg = reg; return c; } bool has_state() const { return true; } };
struct Sens { float state = 0; bool hs = false; bool has_state() const { return hs; } };
struct Bin { bool state = false; bool has_state() const { return true; } };
static Sens acrel_current_l1, acrel_current_l2, acrel_current_l3, linky_pref, linky_urms1;
static Bin linky_depassement_puissance;
static Reg etek_epc2_reg109{109}, etek_epc2_reg89{89};
static int etek_dlb_last_current_a = -1, etek_dlb_enabled = -1, etek_dlb_pending_target_a = 0, etek_dlb_pending_count = 0;
static int dlb_pending_dir = 0, dlb_pause_count = 0, dlb_resume_count = 0, dlb_target_a = 0, dlb_frozen = 0, dlb_stale = 0;
static uint32_t dlb_acrel_l1_ms = 0, dlb_acrel_l2_ms = 0, dlb_acrel_l3_ms = 0, dlb_linky_ms = 0;
static float dlb_headroom_a = 0, dlb_limit_a = 0, dlb_total_a = 0;
'''

TAIL = r'''
extern "C" {
void h_reset() {
  g_writes.clear(); g_now = 0;
  etek_dlb_last_current_a = -1; etek_dlb_enabled = -1; etek_dlb_pending_target_a = 0; etek_dlb_pending_count = 0;
  dlb_pending_dir = 0; dlb_pause_count = 0; dlb_resume_count = 0; dlb_target_a = 0; dlb_frozen = 0; dlb_stale = 0;
  dlb_acrel_l1_ms = dlb_acrel_l2_ms = dlb_acrel_l3_ms = dlb_linky_ms = 0;
  acrel_current_l1 = Sens(); acrel_current_l2 = Sens(); acrel_current_l3 = Sens(); linky_pref = Sens(); linky_urms1 = Sens();
}
void h_verbose(int v) { g_verbose = v; }
void h_set_time(uint32_t t) { g_now = t; }
void h_feed_acrel(float l1, float l2, float l3) {
  acrel_current_l1.state = l1; acrel_current_l1.hs = true; dlb_acrel_l1_ms = g_now;
  acrel_current_l2.state = l2; acrel_current_l2.hs = true; dlb_acrel_l2_ms = g_now;
  acrel_current_l3.state = l3; acrel_current_l3.hs = true; dlb_acrel_l3_ms = g_now;
}
void h_feed_linky(float pref) { linky_pref.state = pref; linky_pref.hs = true; dlb_linky_ms = g_now; linky_urms1.state = 233; linky_urms1.hs = true; }
void h_cycle() { etek_dlb_cycle(); }
int h_nwrites() { return (int) g_writes.size(); }
int h_write_reg(int i) { return g_writes[i].reg; }
double h_write_val(int i) { return g_writes[i].val; }
uint32_t h_write_t(int i) { return g_writes[i].t; }
int h_get(int what) {
  switch (what) {
    case 0: return etek_dlb_last_current_a;
    case 1: return etek_dlb_enabled;
    case 2: return etek_dlb_pending_count;
    case 3: return dlb_pending_dir;
    case 4: return dlb_pause_count;
    case 5: return dlb_resume_count;
    case 6: return dlb_frozen;
    case 7: return dlb_stale;
    case 8: return dlb_target_a;
  }
  return -999;
}
float h_getf(int what) { return what == 0 ? dlb_headroom_a : (what == 1 ? dlb_limit_a : dlb_total_a); }
}
'''


def extract_lambda(yaml_text, marker):
    lines = yaml_text.split("\n")
    i0 = next(i for i, l in enumerate(lines) if marker in l)
    indent = len(lines[i0]) - len(lines[i0].lstrip())
    body = []
    for l in lines[i0:]:
        if l.strip() and (len(l) - len(l.lstrip())) < indent:
            break
        body.append(l)
    return textwrap.dedent("\n".join(body)).rstrip() + "\n"


def build(cfg_text, marker, out_name, workdir):
    body = extract_lambda(cfg_text, marker)
    src = PRELUDE + "\nstatic void etek_dlb_cycle() {\n" + body + "}\n" + TAIL
    cpp = f"{workdir}/{out_name}.cpp"
    open(cpp, "w", encoding="utf-8").write(src)
    r = subprocess.run(["g++", "-std=c++17", "-O1", "-Wall", "-Wno-unused-variable", "-Wno-unused-but-set-variable",
                        "-Wno-unused-function", "-fPIC", "-shared", cpp, "-o", f"{workdir}/{out_name}.so"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr)
        sys.exit("compile failed")
    if r.stderr.strip():
        print("compiler warnings:\n" + r.stderr)
    lib = ctypes.CDLL(f"{workdir}/{out_name}.so")
    lib.h_write_val.restype = ctypes.c_double
    lib.h_getf.restype = ctypes.c_float
    lib.h_feed_acrel.argtypes = [ctypes.c_float] * 3
    lib.h_feed_linky.argtypes = [ctypes.c_float]
    lib.h_set_time.argtypes = [ctypes.c_uint32]
    lib.h_write_t.restype = ctypes.c_uint32
    return lib
