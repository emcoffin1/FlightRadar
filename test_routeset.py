

ranges = [{"range": 540}, {"range": 539}, {"range": 1000}]
minimum_range = min(aircraft["range"] for aircraft in ranges)

print(minimum_range)
