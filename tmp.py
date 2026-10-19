
#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(".")

pattern = re.compile(
    r"// SPDX-License-Identifier: Apache-2\.0[ \t]*\r?\n"
    r"// See LICENSE file for details\.[ \t]*"
)

replacement = (
    "// Copyright (c) 2026, UChicago Argonne, LLC\n"
    "// All Rights Reserved\n"
    "// SPDX-License-Identifier: BSD-3-Clause\n"
    "// See LICENSE file for details."
)

for path in ROOT.rglob("*.scala"):
    text = path.read_text()
    updated, count = pattern.subn(replacement, text, count=1)

    if count:
        path.write_text(updated)
        print(f"Updated: {path}")
