from pokebot.static.dexnav_target_nav import steering_command

# Exact tutorial geometry from hardware support: preserve HF91 stick authority
# but extend the continuous far-distance assertion.
c = steering_command(1791.0, 2349.0, 1701.0, 2331.0)
assert c["x"] == -0.55, c
assert c["y"] == 0.32, c
assert c["hold_ms"] == 2200, c

# Vertical dead-zone recovery remains intact.
c1 = steering_command(1770.0, 2349.0, 1701.0, 2331.0, vertical_stall=1)
c2 = steering_command(1770.0, 2349.0, 1701.0, 2331.0, vertical_stall=2)
assert c1["y"] >= 0.40, c1
assert c2["y"] > c1["y"], (c1, c2)

# Corrections still shorten near contact.
mid = steering_command(1735.0, 2338.0, 1701.0, 2331.0)
near = steering_command(1712.0, 2334.0, 1701.0, 2331.0)
assert mid["hold_ms"] < c["hold_ms"], (mid, c)
assert near["hold_ms"] < mid["hold_ms"], (near, mid)
print("HF92 DEXNAV FLUID STEERING SELF TEST: PASS")
