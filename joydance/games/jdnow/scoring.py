"""Pure Python implementation of the JDN phone motion scorer."""
from bisect import bisect_left, bisect_right
from functools import lru_cache
import struct
import math


SUPPORTED_MEASURES = {50, 51, 52, 56, 61}
SCORING_WINDOW_MS = 75
MOTION_EDGE_TOLERANCE_MS = 30
_FLOAT32 = struct.Struct('<f')
_FLOAT32_MIN_NORMAL = 2.0 ** -126


def float32(value):
    return _FLOAT32.unpack(_FLOAT32.pack(value))[0]


_MIN_MOTION_ENERGY = float32(0.3)


def ratio_score(distance, low_threshold, high_threshold):
    distance, low_threshold, high_threshold = map(float32, (distance, low_threshold, high_threshold))
    if low_threshold == -1 or high_threshold == -1:
        return 0.0
    numerator = float32(distance - high_threshold)
    denominator = float32(low_threshold - high_threshold)
    # ARM floating-point division returns NaN or infinity for a zero denominator.
    ratio = numerator / denominator if denominator else (
        float('nan') if not numerator else float('inf') if numerator > 0 else -float('inf'))
    if ratio < 0:
        return 0.0
    return 1.0 if ratio > 1 else float32(ratio)


def rate_score(raw_score, energy_amount, energy_factor):
    raw_score, energy_amount, energy_factor = map(float32, (raw_score, energy_amount, energy_factor))
    if energy_amount < _MIN_MOTION_ENERGY:
        return 0.0, 0
    if raw_score < float32(0.25):
        return (float32(0.1), 1) if energy_factor > float32(0.6) else (0.0, 0)
    if raw_score < float32(0.5):
        return raw_score, 1
    if raw_score < float32(0.75):
        return raw_score, 2
    return (float32(0.74), 2) if energy_factor < float32(0.4) else (raw_score, 3)


def read_classifier(data):
    if len(data) < 8 or len(data) > 1024 * 1024:
        raise ValueError('Invalid classifier size')
    endian = '<' if struct.unpack_from('<I', data)[0] == 1 else '>'
    marker, version = struct.unpack_from(endian + 'II', data)
    if marker != 1 or version not in (5, 6, 7, 8):
        raise ValueError('Unsupported classifier header')
    offset = 8 if version < 7 else 0
    header_size = 244 - offset
    if len(data) < header_size:
        raise ValueError('Truncated classifier header')
    duration, low, high = struct.unpack_from(endian + 'fff', data, 200)
    autocorrelation, direction_impact = struct.unpack_from(endian + 'ff', data, 212) if version >= 7 else (0.7, 0.05)
    bitfield = struct.unpack_from(endian + 'Q', data, 220 - offset)[0]
    flags, count, energy_count = struct.unpack_from(endian + 'IiI', data, 228 - offset)
    sub_count = struct.unpack_from(endian + 'I', data, 240)[0] if version == 8 else 1
    dimension = abs(count)
    if not 1 <= dimension <= 255 or not 0 <= energy_count <= 255 or sub_count != 1:
        raise ValueError('Unsupported classifier dimensions')
    covariance_count = dimension if count > 0 else dimension * (dimension + 1) // 2
    if len(data) != header_size + 4 * (dimension + covariance_count + energy_count):
        raise ValueError('Invalid classifier data length')
    values = struct.unpack_from(endian + 'f' * ((len(data) - header_size) // 4), data, header_size)
    if not all(math.isfinite(v) for v in (*values, duration, low, high, autocorrelation, direction_impact)):
        raise ValueError('Non-finite classifier parameter')
    if not 0 < duration < 60:
        raise ValueError('Invalid classifier duration')
    # Either -1 threshold disables the ratio in the native scorer.
    if ((low != -1 and high != -1 and high <= low)
            or energy_count != 2 or any(value <= 0 for value in values[-2:])):
        raise ValueError('Invalid classifier scoring parameters')
    if count > 0 and any(value < 0 for value in values[dimension:dimension + covariance_count]):
        raise ValueError('Invalid classifier covariance')
    parts = 10 if version == 5 else int(float32(float32(duration * 30) / float32(2.49)))
    measures = [key for key in range(62) if bitfield & (1 << key)]
    if not 1 <= parts <= 255 or any(m not in SUPPORTED_MEASURES for m in measures) or len(measures) * parts != dimension:
        raise ValueError('Unsupported classifier measures')
    return dict(version=version, duration=duration, low=low, high=high,
                autocorrelation=autocorrelation, direction_impact=direction_impact,
                flags=flags, parts=parts, measures=measures,
                means=values[:dimension], weights=values[dimension:dimension + covariance_count],
                energy=values[dimension + covariance_count:], diagonal=count > 0)


@lru_cache(maxsize=32)
def _timing_grid(duration_ms):
    points = []
    progress = 0.0
    step = 1 / ((duration_ms / 1000) * 100)
    while progress <= 1:
        points.append(progress)
        progress += step
    return tuple(points), tuple(map(float32, points))


def resample(samples, start_time, duration_ms):
    if not 2 <= len(samples) <= 20000 or not 0 < duration_ms <= 60000 or not math.isfinite(start_time):
        raise ValueError('Insufficient motion samples or invalid duration')
    normalized = []
    for sample in samples:
        if len(sample) != 4:
            raise ValueError('Invalid motion sample')
        timestamp, x, y, z = sample
        if not (math.isfinite(timestamp) and math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise ValueError('Invalid motion sample')
        if abs(x) > 1000 or abs(y) > 1000 or abs(z) > 1000:
            raise ValueError('Invalid motion range')
        progress = (timestamp - start_time) / duration_ms
        if normalized and progress < normalized[-1][0]:
            raise ValueError('Motion samples are out of order')
        normalized.append((progress, x, y, z))

    result = []
    index, last = 0, len(normalized) - 1
    points, rounded_points = _timing_grid(duration_ms)
    for progress, rounded in zip(points, rounded_points):
        while index < last and normalized[index + 1][0] <= progress:
            index += 1
        left, x, y, z = normalized[index]
        right, next_x, next_y, next_z = normalized[min(index + 1, last)]
        ratio = (progress - left) / (right - left) if left != right else 0.5
        result.append((rounded, float32(x + ratio * (next_x - x)), float32(y + ratio * (next_y - y)),
                       float32(z + ratio * (next_z - z))))
    return result


def _magnitude(x, y, z):
    return float32(math.sqrt(float32(z * z + float32(x * x + float32(y * y)))))


def motion_features(classifier, samples, start_time, duration_ms):
    sampled = resample(samples, start_time, duration_ms)
    magnitudes = []
    speeds = []
    previous = None
    for progress, x, y, z in sampled:
        magnitudes.append(_magnitude(x, y, z))
        if previous is None:
            speeds.append(0.0)
        else:
            previous_progress, previous_x, previous_y, previous_z = previous
            step = float32(progress - previous_progress)
            speeds.append(_magnitude(float32(float32(x - previous_x) / step),
                                     float32(float32(y - previous_y) / step),
                                     float32(float32(z - previous_z) / step)))
        previous = (progress, x, y, z)

    features = []
    _, times = _timing_grid(duration_ms)
    parts = classifier['parts']
    width = float32(float32(0.96666) / parts)
    bounds = []
    for part in range(parts):
        lower = float32(width * part + float32(0.01667))
        upper = float32(lower + width)
        bounds.append((bisect_left(times, lower), bisect_right(times, upper)))
    signals = {56: magnitudes, 61: speeds}
    for measure in classifier['measures']:
        if 50 <= measure <= 52:
            # Keep the covariance positions for direction features that scoring zeroes out.
            features.extend((measure, 0.0, part) for part in range(1, parts + 1))
            continue
        signal = signals[measure]
        for part, (first, end) in enumerate(bounds, 1):
            total = 0.0
            for value in signal[first:end]:
                total = float32(total + value)
            value = float32(total / (end - first)) if end > first else 0.0
            features.append((measure, value, part))
    return magnitudes, features


@lru_cache(maxsize=128)
def _correlation_geometry(duration_ms, lag):
    # Every candidate and controller uses the same resampled time grid for a given duration.
    _, times = _timing_grid(duration_ms)
    previous = float32(float32(times[0] + times[lag]) * 0.5)
    widths = []
    span = 0.0
    for first, second in zip(times[1:], times[lag + 1:]):
        middle = float32(float32(first + second) * 0.5)
        width = float32(middle - previous)
        widths.append(width)
        span = float32(span + width)
        previous = middle
    return tuple(widths), span


def periodic_motion(classifier, magnitudes, duration_ms):
    if classifier['flags'] & 2 or classifier['autocorrelation'] == -1:
        return False
    total = 0.0
    for norm in magnitudes:
        total = float32(total + norm)
    average = float32(total / len(magnitudes))
    centered = [float32(norm - average) for norm in magnitudes]
    _, times = _timing_grid(duration_ms)

    def integral(lag):
        widths, span = _correlation_geometry(duration_ms, lag)
        previous_product = float32(centered[0] * centered[lag])
        area = 0.0
        for i, width in enumerate(widths, 1):
            product = float32(centered[i] * centered[i + lag])
            height = float32(previous_product + product) * 0.5
            # Halving a float32 only needs another rounding step for subnormal results.
            if abs(height) < _FLOAT32_MIN_NORMAL:
                height = float32(height)
            area = float32(height * width + area)
            previous_product = product
        return float32(area / span) if span else float('nan')

    baseline = integral(0)
    if not baseline > 0:
        return False
    lag_time, minimum = float32(0.02), float('inf')
    while lag_time < float32(float32(0.5) + float32(0.001)):
        lag = bisect_right(times, lag_time)
        if lag >= len(times) - 1:
            return False
        correlation = float32(integral(lag) / baseline)
        minimum = min(minimum, correlation)
        if correlation > classifier['autocorrelation'] and minimum < 0:
            return True
        lag_time = float32(lag_time + float32(0.02))
    return False


def _score_before_periodicity(classifier, samples, start_time, duration_ms):
    magnitudes, features = motion_features(classifier, samples, start_time, duration_ms)
    differences = [0.0 if 50 <= measure <= 52 else float32(value - mean)
                   for (measure, value, _), mean in zip(features, classifier['means'])]
    count = sum(not 50 <= measure <= 52 for measure, _, _ in features)
    if not count:
        raise ValueError('Classifier has no scoring measures')
    distance = 0.0
    if classifier['diagonal']:
        for difference, weight in zip(differences, classifier['weights']):
            distance = float32(float32(weight * difference) * difference + distance)
    else:
        weights = iter(classifier['weights'])
        for i, left in enumerate(differences):
            for j in range(i, len(differences)):
                term = float32(float32(left * differences[j]) * next(weights))
                distance = float32(distance + (term if i == j else float32(term + term)))
    if distance < 0:
        raise ValueError('Invalid classifier covariance')
    distance = float32(math.sqrt(float32(distance / count)))
    raw_score = ratio_score(distance, classifier['low'], classifier['high'])
    grouped = []
    for measure in (56, 61):
        values = [value for key, value, _ in features if key == measure]
        if not values:
            raise ValueError('Energy measure is missing')
        total = 0.0
        for value in values:
            total = float32(total + value)
        grouped.append(float32(total / len(values)))
    if len(classifier['energy']) != 2 or any(value <= 0 for value in classifier['energy']):
        raise ValueError('Invalid classifier energy reference')
    amount_weight = float32(0.1)
    amount = float32(float32(1 - amount_weight) * max(0.0, float32(grouped[0] - 1)) +
                     float32(amount_weight * grouped[1]))
    factor_weight = float32(0.667)
    factor = float32(float32(1 - factor_weight) * float32(grouped[0] / classifier['energy'][0]) +
                     float32(factor_weight * float32(grouped[1] / classifier['energy'][1])))
    raw_score = min(1.0, max(0.0, raw_score))
    score, rating = rate_score(raw_score, amount, factor)
    # Below 0.25 the rating already comes from the energy fallback.
    return (score, amount, factor, rating), magnitudes if score >= 0.25 else None


def _finish_score(classifier, result, magnitudes, duration_ms):
    if magnitudes is not None and periodic_motion(classifier, magnitudes, duration_ms):
        _, amount, factor, _ = result
        score, rating = rate_score(0.0, amount, factor)
        return score, amount, factor, rating
    return result


def score_move(classifier, samples, start_time, duration_ms):
    result, magnitudes = _score_before_periodicity(classifier, samples, start_time, duration_ms)
    return _finish_score(classifier, result, magnitudes, duration_ms)


def score_adaptive_move(classifier, samples, start_time, duration_ms, pointer):
    end_time = start_time + duration_ms
    times = [sample[0] for sample in samples]
    # Use the first sample after the deadline, even if a later batch triggered scoring.
    anchor = bisect_right(times, end_time + SCORING_WINDOW_MS)
    candidates = []
    if anchor < len(times):
        oldest = bisect_left(times, times[anchor] - 2 * SCORING_WINDOW_MS)
        for last in range(anchor, oldest - 1, -1):
            start = times[last] - duration_ms
            first = bisect_left(times, start, hi=last)
            if first < last and times[first] <= start + MOTION_EDGE_TOLERANCE_MS:
                result, magnitudes = _score_before_periodicity(classifier, samples[first:last + 1], start, duration_ms)
                candidates.append((result, magnitudes, start - start_time))

    if not candidates:
        # Keep the nominal window when the stream ends before the late samples arrive.
        if (len(times) < 2 or times[0] > start_time + MOTION_EDGE_TOLERANCE_MS
                or times[-1] < end_time - MOTION_EDGE_TOLERANCE_MS):
            return None, pointer, 0
        return score_move(classifier, samples, start_time, duration_ms), pointer, 0

    last_index = len(candidates) - 1
    selected = round(float32(pointer * last_index))
    resolved = {}

    def resolve(index):
        if index not in resolved:
            result, magnitudes, _ = candidates[index]
            resolved[index] = _finish_score(classifier, result, magnitudes, duration_ms)
        return resolved[index]

    result = resolve(selected)
    best_index, best_score = selected, result[0]
    score_bounds = [candidate[0][0] for candidate in candidates]
    # Periodicity can only lower the score, so candidates below the best known score can be skipped.
    for index in sorted(range(len(candidates)), key=score_bounds.__getitem__, reverse=True):
        bound = score_bounds[index]
        if bound < best_score:
            break
        if bound == best_score and index >= best_index:
            continue
        score = resolve(index)[0]
        if score > best_score or (score == best_score and index < best_index):
            best_index, best_score = index, score

    # An unresolved tie can still be lowered, which matters when every confirmed score is equal.
    if (min(score_bounds) < best_score or any(score[0] < best_score for score in resolved.values())
            or any(resolve(index)[0] < best_score for index in range(len(candidates)))):
        # The app uses the current pointer for this move and learns from the first best window.
        target = float32(best_index / last_index)
        pointer = float32(pointer + float32(float32(target - pointer) * float32(0.2)))
    return result, pointer, candidates[selected][2]
