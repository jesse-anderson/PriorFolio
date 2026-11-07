# SPDX-License-Identifier: GPL-3.0-only
import priorfolio
import priorfolio_gui  # Load Tk's native dependencies before resetting DLL lookup.
from runtime import prepare

prepare()
priorfolio.main()
