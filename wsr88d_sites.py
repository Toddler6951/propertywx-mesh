"""WSR-88D radar site locations for nearest-radar selection.

Source: NWS Operational Site List for the WSR-88D network. Coordinates are
the antenna location (lat, lon, elev_ft). Accuracy is within tens of meters
which is more than enough for nearest-neighbor selection within ~150 mi.

Coverage: CONUS only. Alaska, Hawaii, Puerto Rico, and Guam radars are
omitted since the property-claim app is US-mainland focused.
"""

# id -> (name, lat, lon, elev_ft)
WSR88D_SITES = {
    # Northeast / Mid-Atlantic
    "KCBW": ("Caribou ME",                   46.0392,  -67.8067,  746),
    "KGYX": ("Portland ME",                  43.8914,  -70.2569,  409),
    "KCXX": ("Burlington VT",                44.5111,  -73.1664,  317),
    "KBOX": ("Boston MA",                    41.9558,  -71.1369,  121),
    "KENX": ("Albany NY",                    42.5864,  -74.0639, 1826),
    "KBGM": ("Binghamton NY",                42.1997,  -75.9847, 1606),
    "KBUF": ("Buffalo NY",                   42.9489,  -78.7369,  693),
    "KTYX": ("Montague NY",                  43.7558,  -75.6800, 1846),
    "KOKX": ("New York City NY",             40.8656,  -72.8639,   85),
    "KCCX": ("State College PA",             40.9233,  -78.0036, 2405),
    "KDIX": ("Mt Holly NJ",                  39.9469,  -74.4108,  149),
    "KPBZ": ("Pittsburgh PA",                40.5317,  -80.2181, 1185),
    "KLWX": ("Sterling VA / DC",             38.9761,  -77.4775,  272),
    "KAKQ": ("Wakefield VA",                 36.9839,  -77.0075,  113),
    "KFCX": ("Roanoke VA",                   37.0242,  -80.2742, 2868),
    "KRLX": ("Charleston WV",                38.3111,  -81.7228, 1080),

    # Great Lakes / Midwest
    "KILN": ("Wilmington OH",                39.4203,  -83.8217, 1056),
    "KCLE": ("Cleveland OH",                 41.4131,  -81.8597,  763),
    "KDTX": ("Detroit MI",                   42.7000,  -83.4717, 1072),
    "KAPX": ("Gaylord MI",                   44.9069,  -84.7197, 1464),
    "KGRR": ("Grand Rapids MI",              42.8939,  -85.5447,  778),
    "KMQT": ("Marquette MI",                 46.5311,  -87.5483, 1411),
    "KIWX": ("North Webster IN",             41.3589,  -85.7000,  960),
    "KIND": ("Indianapolis IN",              39.7075,  -86.2803,  790),
    "KVWX": ("Evansville IN",                38.2603,  -87.7244,  390),
    "KILX": ("Lincoln IL",                   40.1503,  -89.3367,  672),
    "KLOT": ("Chicago/Romeoville IL",        41.6044,  -88.0847,  663),
    "KMKX": ("Milwaukee WI",                 42.9678,  -88.5506,  958),
    "KGRB": ("Green Bay WI",                 44.4983,  -88.1114,  682),
    "KARX": ("La Crosse WI",                 43.8228,  -91.1908, 1276),
    "KDLH": ("Duluth MN",                    46.8369,  -92.2097, 1428),
    "KMPX": ("Minneapolis MN",               44.8489,  -93.5656,  946),
    "KDMX": ("Des Moines IA",                41.7311,  -93.7228, 1058),
    "KDVN": ("Davenport IA",                 41.6117,  -90.5808,  751),
    "KEAX": ("Kansas City MO",               38.8103,  -94.2644,  993),
    "KSGF": ("Springfield MO",               37.2353,  -93.4006, 1268),
    "KLSX": ("St Louis MO",                  38.6989,  -90.6828,  608),

    # Northern Plains
    "KOAX": ("Omaha NE",                     41.3203,  -96.3669, 1148),
    "KLNX": ("North Platte NE",              41.9578, -100.5764, 2970),
    "KUEX": ("Hastings NE",                  40.3208,  -98.4419, 1971),
    "KFGF": ("Grand Forks ND",               47.2581,  -97.1500,  906),
    "KMVX": ("Mayville ND",                  47.5278,  -97.3253,  986),
    "KBIS": ("Bismarck ND",                  46.7708, -100.7606, 1660),
    "KMBX": ("Minot AFB ND",                 48.3925, -100.8642, 1493),
    "KABR": ("Aberdeen SD",                  45.4558,  -98.4131, 1302),
    "KFSD": ("Sioux Falls SD",               43.5878,  -96.7294, 1414),
    "KUDX": ("Rapid City SD",                44.1250, -102.8297, 3016),

    # Southern Plains (hail belt core)
    "KGLD": ("Goodland KS",                  39.3667, -101.7000, 3651),
    "KDDC": ("Dodge City KS",                37.7611,  -99.9689, 2590),
    "KICT": ("Wichita KS",                   37.6544,  -97.4431, 1335),
    "KTWX": ("Topeka KS",                    38.9969,  -96.2325, 1367),
    "KTLX": ("Oklahoma City OK",             35.3331,  -97.2775, 1213),
    "KVNX": ("Vance AFB OK",                 36.7406,  -98.1278, 1210),
    "KINX": ("Tulsa OK",                     36.1750,  -95.5644,  668),
    "KFDR": ("Frederick OK",                 34.3622,  -98.9764, 1267),

    # Texas
    "KAMA": ("Amarillo TX",                  35.2333, -101.7094, 3587),
    "KLBB": ("Lubbock TX",                   33.6541, -101.8141, 3270),
    "KDYX": ("Dyess AFB TX",                 32.5384,  -99.2542, 1517),
    "KMAF": ("Midland TX",                   31.9433, -102.1894, 2851),
    "KFWS": ("Dallas / Fort Worth TX",       32.5731,  -97.3031,  683),
    "KGRK": ("Fort Hood TX",                 30.7219,  -97.3831,  538),
    "KEWX": ("Austin / San Antonio TX",      29.7041,  -98.0286,  632),
    "KSJT": ("San Angelo TX",                31.3711, -100.4925, 1890),
    "KCRP": ("Corpus Christi TX",            27.7842,  -97.5111,   45),
    "KBRO": ("Brownsville TX",               25.9159,  -97.4189,   23),
    "KHGX": ("Houston / Galveston TX",       29.4719,  -95.0792,   18),
    "KEPZ": ("El Paso TX",                   31.8731, -106.6975, 4104),

    # Mountain West
    "KABX": ("Albuquerque NM",               35.1497, -106.8239, 5870),
    "KFDX": ("Cannon AFB NM",                34.6342, -103.6189, 4650),
    "KHDX": ("Holloman AFB NM",              33.0764, -106.1225, 4222),
    "KFSX": ("Flagstaff AZ",                 34.5744, -111.1981, 7417),
    "KIWA": ("Phoenix AZ",                   33.2891, -111.6700, 1353),
    "KEMX": ("Tucson AZ",                    31.8939, -110.6303, 5202),
    "KYUX": ("Yuma AZ",                      32.4953, -114.6567,  174),
    "KICX": ("Cedar City UT",                37.5908, -112.8625, 10600),
    "KMTX": ("Salt Lake City UT",            41.2628, -112.4478, 6460),
    "KCBX": ("Boise ID",                     43.4906, -116.2356, 3104),
    "KSFX": ("Pocatello ID",                 43.1056, -112.6861, 4474),
    "KMSX": ("Missoula MT",                  47.0414, -113.9858, 7855),
    "KGGW": ("Glasgow MT",                   48.2064, -106.6253, 2276),
    "KTFX": ("Great Falls MT",               47.4597, -111.3853, 3714),
    "KBLX": ("Billings MT",                  45.8538, -108.6064, 3598),
    "KRIW": ("Riverton WY",                  43.0664, -108.4772, 5568),
    "KCYS": ("Cheyenne WY",                  41.1519, -104.8061, 6128),
    "KFTG": ("Denver CO",                    39.7867, -104.5458, 5497),
    "KGJX": ("Grand Junction CO",            39.0622, -108.2139, 9991),
    "KPUX": ("Pueblo CO",                    38.4594, -104.1817, 5249),

    # Pacific
    "KATX": ("Seattle WA",                   48.1947, -122.4956,  494),
    "KOTX": ("Spokane WA",                   47.6803, -117.6261, 2384),
    "KLGX": ("Langley Hill WA",              47.1167, -124.1067,  246),
    "KRTX": ("Portland OR",                  45.7150, -122.9647, 1572),
    "KMAX": ("Medford OR",                   42.0814, -122.7172, 7513),
    "KPDT": ("Pendleton OR",                 45.6906, -118.8528, 1515),
    "KESX": ("Las Vegas NV",                 35.7008, -114.8917, 4867),
    "KRGX": ("Reno NV",                      39.7542, -119.4622, 8299),
    "KLRX": ("Elko NV",                      40.7397, -116.8025, 6744),
    "KBHX": ("Eureka CA",                    40.4983, -124.2922, 2402),
    "KBBX": ("Beale AFB CA",                 39.4961, -121.6314,  173),
    "KMUX": ("Bay Area CA",                  37.1553, -121.8983, 3469),
    "KHNX": ("San Joaquin Valley CA",        36.3142, -119.6322,  243),
    "KVTX": ("Los Angeles CA",               34.4117, -119.1786, 2726),
    "KSOX": ("Santa Ana Mtn CA",             33.8181, -117.6356, 3025),
    "KNKX": ("San Diego CA",                 32.9189, -117.0419,  955),

    # Southeast
    "KMHX": ("Morehead City NC",             34.7758,  -76.8761,   31),
    "KRAX": ("Raleigh NC",                   35.6653,  -78.4900,  348),
    "KLTX": ("Wilmington NC",                33.9892,  -78.4292,   64),
    "KGSP": ("Greer SC",                     34.8833,  -82.2200,  940),
    "KCAE": ("Columbia SC",                  33.9486,  -81.1183,  211),
    "KCLX": ("Charleston SC",                32.6556,  -81.0422,   97),
    "KFFC": ("Atlanta GA",                   33.3636,  -84.5658,  858),
    "KJGX": ("Robins AFB GA",                32.6753,  -83.3514,  521),
    "KVAX": ("Moody AFB GA",                 30.8903,  -83.0019,  179),
    "KJAX": ("Jacksonville FL",              30.4847,  -81.7019,   33),
    "KMLB": ("Melbourne FL",                 28.1131,  -80.6539,   99),
    "KAMX": ("Miami FL",                     25.6111,  -80.4128,   45),
    "KBYX": ("Key West FL",                  24.5975,  -81.7031,    8),
    "KTBW": ("Tampa Bay FL",                 27.7056,  -82.4017,   41),
    "KTLH": ("Tallahassee FL",               30.3975,  -84.3289,   63),
    "KEVX": ("Eglin AFB FL",                 30.5639,  -85.9214,  140),
    "KMOB": ("Mobile AL",                    30.6794,  -88.2400,  208),
    "KBMX": ("Birmingham AL",                33.1722,  -86.7700,  645),
    "KHTX": ("Hytop AL",                     34.9306,  -86.0833, 1760),
    "KMXX": ("Maxwell AFB AL",               32.5367,  -85.7900,  400),
    "KOHX": ("Nashville TN",                 36.2472,  -86.5625,  579),
    "KMRX": ("Knoxville TN",                 36.1686,  -83.4017, 1337),
    "KNQA": ("Memphis TN",                   35.3447,  -89.8731,  282),
    "KGWX": ("Columbus AFB MS",              33.8967,  -88.3294,  476),
    "KDGX": ("Jackson MS",                   32.2800,  -89.9842,  493),
    "KLZK": ("Little Rock AR",               34.8364,  -92.2622,  568),
    "KSRX": ("Western Arkansas",             35.2906,  -94.3619, 1953),
    "KLIX": ("New Orleans LA",               30.3367,  -89.8256,   24),
    "KLCH": ("Lake Charles LA",              30.1253,  -93.2161,   13),
    "KSHV": ("Shreveport LA",                32.4506,  -93.8414,  273),
    "KPOE": ("Fort Polk LA",                 31.1556,  -92.9758,  407),
    "KPAH": ("Paducah KY",                   37.0683,  -88.7719,  392),
    "KHPX": ("Fort Campbell KY",             36.7367,  -87.2856,  573),
    "KLVX": ("Louisville KY",                37.9753,  -85.9439,  719),
    "KJKL": ("Jackson KY",                   37.5908,  -83.3131, 1364),
}


def haversine_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in statute miles."""
    from math import asin, cos, radians, sin, sqrt
    R = 3958.8  # mean Earth radius, miles
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


def nearest_radar(lat, lon, max_dist_mi=150.0):
    """Return (radar_id, dist_mi, name, radar_lat, radar_lon, radar_elev_ft)
    for the closest WSR-88D within max_dist_mi of (lat, lon), or None."""
    best = None
    for rid, (name, rlat, rlon, relev) in WSR88D_SITES.items():
        d = haversine_mi(lat, lon, rlat, rlon)
        if best is None or d < best[1]:
            best = (rid, d, name, rlat, rlon, relev)
    if best and best[1] <= max_dist_mi:
        return best
    return None
