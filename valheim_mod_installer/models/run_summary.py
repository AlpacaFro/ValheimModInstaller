from dataclasses import dataclass, field
from typing import List


@dataclass
class RunSummary:
    downloaded: int = 0
    gathered_dlls: int = 0
    installed_files: int = 0
    backed_up_files: int = 0
    failures: List[str] = field(default_factory=list)
    dependency_warnings: List[dict] = field(default_factory=list)
