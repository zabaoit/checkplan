"""Fallback caller inventory when GitNexus native parser cannot start."""
import ast
import subprocess
from pathlib import Path
root = Path(__file__).resolve().parents[1]
targets = {'ChangePasswordJob', 'ChangePasswordJobManager', 'get_change_password_manager',
           '_broadcast_job', 'add_jobs', '_run_job', 'clear_finished', 'remove_job', 'retry_job',
           'to_dict', 'to_dict_full', 'AddChangePasswordJobsRequest', 'add_change_password_jobs'}
paths = subprocess.check_output(['git','ls-files','*.py'],cwd=root,text=True).splitlines()
for name in sorted(targets):
    hits = []
    for path in paths:
        try:
            tree=ast.parse((root/path).read_text(encoding='utf-8-sig'))
        except (SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn=node.func
                called=fn.id if isinstance(fn,ast.Name) else fn.attr if isinstance(fn,ast.Attribute) else ''
                if called == name:
                    hits.append(f'{path}:{node.lineno}')
    # Attribute names are intentionally conservative; other managers share method names.
    print(name, len(hits), ', '.join(hits[:18]))
