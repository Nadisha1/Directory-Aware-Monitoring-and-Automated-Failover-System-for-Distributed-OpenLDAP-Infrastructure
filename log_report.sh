#!/bin/bash
# log_report.sh - summary report of the controller audit log and the
# experiment result files. Run on ctrl:
#   bash /vagrant/controller/log_report.sh
# The control panel "Log Report" button runs this and saves the output
# to the reports\ folder on Windows.

LOG="${AUDIT_LOG:-/var/log/remediation-controller/audit.log}"
CSV_GLOB="${REPORT_CSV_GLOB:-/home/vagrant/gui_*.csv /tmp/demo_*.csv}"

readlog() {
  if [ -r "$LOG" ]; then cat "$LOG"; else sudo cat "$LOG" 2>/dev/null; fi
}

echo "=================================================================="
echo " OpenLDAP remediation - LOG REPORT"
echo " Generated (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Host: $(hostname)"
echo "=================================================================="

DATA="$(readlog)"
if [ -z "$DATA" ]; then
  echo
  echo "No audit log found, or it is empty: $LOG"
else
  TOTAL=$(printf '%s\n' "$DATA" | wc -l)
  FIRST=$(printf '%s\n' "$DATA" | head -n 1 | awk '{print $1}')
  LAST=$(printf '%s\n' "$DATA" | tail -n 1 | awk '{print $1}')
  echo
  echo "1. AUDIT LOG SUMMARY"
  echo "   File:          $LOG"
  echo "   Entries:       $TOTAL"
  echo "   First entry:   $FIRST"
  echo "   Last entry:    $LAST"

  echo
  echo "2. ACTIONS BY TYPE"
  printf '%s\n' "$DATA" | awk '{for(i=1;i<=NF;i++) if($i ~ /^action=/){sub(/^action=/,"",$i); c[$i]++}} END{for(k in c) printf "   %-26s %d\n", k, c[k]}' | sort

  echo
  echo "3. FENCE EVENTS PER NODE"
  printf '%s\n' "$DATA" | awk '{a="";n="";for(i=1;i<=NF;i++){if($i ~ /^action=/)a=$i; if($i ~ /^node=/)n=$i}; if(a=="action=FENCE"){sub(/^node=/,"",n); c[n]++}} END{for(k in c) printf "   %-10s %d\n", k, c[k]}' | sort

  echo
  echo "4. LAST 15 ENTRIES"
  printf '%s\n' "$DATA" | tail -n 15 | sed 's/^/   /'
fi

echo
echo "5. EXPERIMENT RESULT FILES"
python3 - "$CSV_GLOB" <<'PY'
import csv, glob, os, statistics, sys, time
files = []
for pat in sys.argv[1].split():
    files += sorted(glob.glob(pat))
if not files:
    print("   (no result files found)")
for f in files:
    try:
        rows = list(csv.DictReader(open(f, newline="")))
    except Exception as e:
        print("   %s: cannot read (%s)" % (f, e))
        continue
    mt = time.strftime("%Y-%m-%d %H:%M", time.gmtime(os.path.getmtime(f)))
    print("   %s  rows=%d  modified(UTC)=%s" % (os.path.basename(f), len(rows), mt))
    if rows and "scenario" in rows[0] and "outcome" in rows[0]:
        sc = {}
        for r in rows:
            sc.setdefault(r["scenario"], []).append(r)
        for s, rs in sorted(sc.items()):
            ok = [r for r in rs if r["outcome"] == "success"]
            line = "      %-12s success %d/%d" % (s, len(ok), len(rs))
            try:
                v = [float(r["total_mttr_s"]) for r in ok if r.get("total_mttr_s")]
                if v:
                    line += "   mean MTTR %.1f s" % statistics.mean(v)
            except Exception:
                pass
            print(line)
PY

echo
echo "Note: numbers in the dissertation must come from these raw files."
echo "End of report."
