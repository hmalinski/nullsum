#!/bin/bash
# usage: run_unittests.sh BINARY LABEL OUTDIR [ENVVAR=1]
BIN=$1; LABEL=$2; OUT=$3; ENVSET=$4
mkdir -p $OUT/logs
SUITES="tx.LoanValidation tx.LoanCashBasis tx.LoanInvariants tx.LoanRounding tx.LoanPay tx.LendingBatch tx.LoanMisc tx.LoanBroker tx.LoanPermissionedDomain tx.LoanCoverFreezeAuth tx.LoanLifecycle tx.LoanSet tx.LoanSecurity app.LoanManageFixedPrecision app.LoanSetFixedPrecision app.LoanPayFixedPrecision app.VaultCoarsenedFixedPrecision app.LendingHelpers app.VaultFixedPrecision app.VaultLifecycle app.VaultClosedEnded app.VaultInvariantPrecision app.VaultTransactorPrecision app.VaultBugs app.VaultClawback app.VaultShares app.VaultHelpers app.VaultValidation app.VaultRPC app.VaultFreeze app.VaultDomain app.VaultSoleShareholder app.VaultScale app.InvariantsVault app.InvariantsMisc app.InvariantsMPT app.InvariantsPseudoAccount"
run_one() {
  s=$1
  log=$OUT/logs/$LABEL.$s.log
  start=$(date +%s)
  if [ -n "$ENVSET" ]; then env $ENVSET timeout 1500 $BIN --unittest=xrpl.$s > $log 2>&1; else timeout 1500 $BIN --unittest=xrpl.$s > $log 2>&1; fi
  rc=$?
  end=$(date +%s)
  summ=$(grep -E 'suites?, [0-9]+ cases?, [0-9]+ tests? total' $log | tail -1)
  # Only record an assertion line when the suite actually FAILED (rc!=0), matching the real
  # abort format "Assertion ... failed" (not a test-case name that merely contains "assert"),
  # and strip any absolute build path to a repo-relative one.
  if [ $rc -ne 0 ]; then
    asrt=$(grep -m1 -E 'Assertion .* failed' $log | sed 's#.*/src/libxrpl/#src/libxrpl/#' | cut -c1-160)
  else
    asrt=""
  fi
  echo -e "$s\t$rc\t$((end-start))s\t$summ\t$asrt"
}
export -f run_one; export BIN LABEL OUT ENVSET
printf '%s\n' $SUITES | xargs -P ${PAR:-3} -I{} bash -c 'run_one {}' | sort > $OUT/$LABEL.tsv
echo "done $LABEL: $(awk -F'\t' '$2==0' $OUT/$LABEL.tsv | wc -l) rc0, $(awk -F'\t' '$2!=0' $OUT/$LABEL.tsv | wc -l) nonzero"
