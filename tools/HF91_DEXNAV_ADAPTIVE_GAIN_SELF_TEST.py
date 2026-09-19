from pokebot.static.dexnav_target_nav import steering_command

# Exact tutorial geometry from hardware support.
c = steering_command(1791.0, 2349.0, 1701.0, 2331.0)
assert c['x'] == -0.55, c
assert c['y'] == 0.32, c
assert c['hold_ms'] == 1600, c

# If hardware reports no Z progress, vertical authority must escalate.
c1 = steering_command(1770.0, 2349.0, 1701.0, 2331.0, vertical_stall=1)
c2 = steering_command(1770.0, 2349.0, 1701.0, 2331.0, vertical_stall=2)
assert c1['y'] >= 0.40, c1
assert c2['y'] > c1['y'], (c1, c2)

# Near target, stay partial and bounded.
c3 = steering_command(1712.0, 2334.0, 1701.0, 2331.0)
assert 0.0 < abs(c3['x']) < 0.6, c3
assert 0.0 < abs(c3['y']) < 0.5, c3
print('HF91 DEXNAV ADAPTIVE GAIN SELF TEST: PASS')
