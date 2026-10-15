# Authors

## Lead author and maintainer

**Thariq Arian Khalfani**
ariankhalfani@gmail.com

Copyright (c) 2026 Thariq Arian Khalfani. Released under the MIT licence — see [LICENSE](LICENSE).

## What this file is for

Not attribution for its own sake. An unsigned binary with no stated author, no company and no version resource
is indistinguishable from malware to every heuristic that looks at it, and Windows Defender is one of them. This
file, [CITATION.cff](CITATION.cff) and the version resource that `myriaxon.launcher_identity` writes into the
installed launcher all exist so that a person — or a scanner — can answer "who made this and where does it come
from" without guessing.

Measured, and it is the whole reason the launcher work exists:

```
$ python tools/verify_binary_identity.py
  CompanyName      : (none)
  ProductName      : (none)
  FileDescription  : (none)
  FileVersion      : (none)
```

A pip-generated console-script launcher carries **no version resource at all**, which is normal for pip and
anomalous for anything else. See [docs/FALSE-POSITIVE.md](docs/FALSE-POSITIVE.md).

## Reporting

Security reports and false-positive reports are both welcome and both treated as defects:

* **Repository issues** — https://github.com/matchu-id/myriaxon-harness/issues
* **Email** — ariankhalfani@gmail.com

If an antivirus product flags this project, that is a bug in the packaging and worth reporting with the
detection name, the file path, and the output of `python tools/verify_binary_identity.py`.
