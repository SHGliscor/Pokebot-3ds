Pokebot3DS-CFW v0p43AL - Capture Trace Diff Workflow
=====================================================

Goal
----
Stop guessing at capture timing.  Compare one hardware-proven successful catch
against one hardware-proven real breakout and find the FIRST RAM state where the
paths genuinely diverge.

Step 1 - successful capture trace
---------------------------------
Run CAPTURE_LIFECYCLE_MAPPER.bat in an ordinary single wild battle.
Let the Ball catch the Pokemon and let the mapper continue through the complete
post-capture sequence to stable overworld if possible.

Expected result in its JSON:
  SUCCESSFUL_CAPTURE_LIFECYCLE_TO_OVERWORLD

Step 2 - real breakout trace
----------------------------
Run CAPTURE_LIFECYCLE_MAPPER.bat again.  When the Ball genuinely breaks out and
the FIGHT / BAG / POKEMON / RUN menu is visibly ready, answer Y to the mapper's
hardware confirmation question.

Expected result in its JSON:
  OPERATOR_CONFIRMED_REAL_BREAKOUT

Never answer Y merely because the console says RAM resembles the command gate.
The visible hardware menu is the breakout ground truth for this experiment.

Step 3 - diff
-------------
Double-click CAPTURE_TRACE_DIFF.bat.

With no arguments it auto-finds the newest successful and breakout lifecycle
JSON files under:
  %APPDATA%\Pokebot-3DS\support

Advanced command-line form:
  python tools\capture_trace_diff.py SUCCESS.json BREAKOUT.json

Output:
  capture_trace_diff_YYYYMMDD_HHMMSS.json
  capture_trace_diff_YYYYMMDD_HHMMSS.txt

What v0p43AL records
--------------------
* nominal 50 ms scalar timeline
* exact mapped battle/FLOW/outer/view/Bag/PK6 state
* 20 KiB RAM window at 0x081FB000
* 4 KiB RAM window at 0x0852FA00
* 0x400-byte live outer/view heap-object snapshots when pointers are valid
* scheduled snapshots from immediately after the throw through T+60 sec
* broad before/after snapshots around every mapper A/B input
* first capture authority and operator-confirmed breakout snapshots

The diff analyzer ranks:
* earliest differing byte addresses
* aligned 32-bit values
* how often each address differs
* whether it stays different after first divergence
* scalar state differences at matching post-throw times
* candidate field::EventBattleReturn object sightings

Static reverse-engineering lead
-------------------------------
The supplied Alpha Sapphire executable contains the real field classes:
  field::EventBattleCall
  field::EventBattleReturn
  field::EventCaptureCall

Static analysis found an EventBattleReturn object vptr candidate 0x005DF09C and
an 8-state update machine reading object offset +0x48.  v0p43AL looks for that
object in captured RAM, but it is NOT production authority until the hardware
trace proves where/when it is live and how its states correspond to capture and
breakout.

Safety
------
* NO game RAM writes.
* Mapper Master Ball eligibility is hard-disabled.
* Exactly one Ball is thrown per mapper run.
* The diff analyzer is completely offline/read-only.
* v0p43AL does NOT change the live wild-hunt capture implementation.
* No Alpha Sapphire game executable or ROM data is included in this package.
