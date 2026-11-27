#!/usr/bin/env python3
"""Validate a native Scout stop receipt across the JSON wire in a disposable DB."""
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    document = json.loads(Path(sys.argv[1]).read_text())
    with tempfile.TemporaryDirectory(prefix='nexus-stop-wire-') as root:
        os.environ['MINO_NEXUS_DATA_DIR'] = root
        from mino_nexus.resources import scout_record_store as records
        from mino_nexus.loop import scout_stop_evidence as evidence
        from mino_nexus.platform.identity import execution_user
        summary, proof = document['summary'], document['proof']
        row = {key: summary[key] for key in ('task_id', 'session_id', 'attempt_id', 'owner_user_id',
                'node_id', 'sn', 'case_id', 'status', 'summary_version')}
        row.update(summary=deepcopy(summary), payload={'platform': 'web'}, context={})
        assert summary['execution_started'] == 'unknown' and summary.get('browser_closed') is not True
        with execution_user(summary['owner_user_id']), patch.object(evidence, '_request', return_value=document['events']):
            proof['retirement_write_audit'] = evidence.audit_writes(row)
            assert records.web_stop_proof_valid(row, proof), proof
            row['context']['resource_stop'] = proof
            assert records.stop_evidence_confirmed(row)
            assert not records.release_evidence_confirmed(row), 'Stop evidence alone must not bypass atomic resolution'
            assert records.web_stop_recovery_candidate(row), 'Timeout without cancel_requested must be scanned'
            altered = deepcopy(proof)
            altered['resource_stop']['process_tree_stop']['process_tree_stopped'] = False
            assert not records.web_stop_proof_valid(row, altered)
            assert row['summary'] == summary
        print('PASS: native Chromium stop JSON -> Nexus sealed-event audit -> recovery eligibility; atomic release still required; UNKNOWN unchanged')
    print('No production DB, historical task, or business action was modified.')


if __name__ == '__main__':
    main()
