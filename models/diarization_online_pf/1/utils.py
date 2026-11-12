import asyncio
import numpy as np

from typing import List, Tuple, Union, Generator

import triton_python_backend_utils as pb_utils

from diarization_protocol import output_pb2
from pyannote.core import Annotation, Segment


def adjust_all_chunks_cumulative(chunks, chunk_duration=None):
        """
        Adjusts a list of chunk results so that the time, frame indices, and seg_idx form a continuous timeline.
        Each segment is expected to have the format:
            [utt, seg_idx, begin_ms, end_ms, begin_frames, end_frames]
        This function does not require you to supply fixed chunk lengths—it calculates offsets
        from the maximum values within each chunk.
        
        Args:
            chunks: A list where each element is a list of segments (each segment is
                    [utt, seg_idx, begin_ms, end_ms, begin_frames, end_frames]) for a chunk.
                    
        Returns:
            A single list of segments with time, frame, and seg_idx values adjusted to be continuous.
        """
        cumulative_time_offset = 0
        cumulative_seg_offset = 0
        all_adjusted = []
        
        for chunk in chunks:
            adjusted_chunk = []
            for seg in chunk:
                seg_adjusted = list(seg)  # Make a copy of the segment
                # Adjust the seg_idx (index 1), time fields (indices 2 & 3), and frame fields (indices 4 & 5)
                seg_adjusted[1] += cumulative_seg_offset       # Adjust seg_idx
                seg_adjusted[2] += cumulative_time_offset        # Adjust begin_ms
                seg_adjusted[3] += cumulative_time_offset        # Adjust end_ms
                adjusted_chunk.append(seg_adjusted)
            all_adjusted.extend(adjusted_chunk)
            
            if chunk:
                # Also update the cumulative seg index offset.
                # We assume seg_idx is zero-based; hence add (max(seg_idx) + 1)
                max_seg_index = max(seg[1] for seg in chunk)
                if chunk_duration is not None:
                    cumulative_time_offset += chunk_duration
                cumulative_seg_offset += (max_seg_index + 1)
                
        return all_adjusted



def process_vad_results(vad_results):
    """
    Process streaming VAD results into a standard list of [start, end] segments.

    The input is a list of chunks (lists), where each element is a dictionary with one key:
    - 1: indicates the start (Begin) of a segment.
    - 2: indicates the end (End) of a segment.
    - 0: indicates a None value. 
            * If a key 0 is found between a Begin (1) and an End (2), it is considered as part
            of the same segment.
            * If a key 0 appears without an open segment, it is ignored.
    
    Returns:
        A list of [start, end] segments.
    """
    # Flatten the list of lists into a single list
    flat_results = [entry for chunk in vad_results for entry in chunk]
    # Get the last dictionary
    last_dict = flat_results[-1]
    if 0 in last_dict:
        # Change key 0 to 2, in case if last segment is NONE, to switch to the END
        last_dict[2] = last_dict.pop(0)

    segments = []
    current_begin = None
    for entry in flat_results:
        # Each dictionary has a single key-value pair.
        key, value = list(entry.items())[0]

        if key == 1:
            if current_begin is not None: # Handling Begin 1, Begin value problems
                current_begin = None
            # Start a new segment.
            # If a segment is already open, we reset it.
            current_begin = value
        elif key == 2:
            # Close the current segment only if a begin exists.
            if current_begin is not None:
                segments.append([current_begin, value])
                current_begin = None
            # If no segment is open, ignore the end marker.
        elif key == 0:
            # If a segment is open, a None marker is just part of it (ignored).
            # If no segment is open, it’s an empty segment so ignore it.
            pass
    
    return segments

def adjust_audiogram_marks(chunks, chunk_duration):
    """
    Adjusts VAD results timestamps by adding a cumulative offset based on chunk_duration.
    
    Each chunk is a list of dictionaries containing time values relative to that chunk.
    The function updates the numeric values in each dictionary by adding the cumulative offset 
    (in milliseconds) so that the timestamps form a continuous timeline across chunks.
    
    The structure of the data is preserved. If a dictionary's value is None, it remains unchanged.
    
    Args:
        chunks (list): A list of chunks, where each chunk is a list of dictionaries.
                    For example:
                        [
                        [{1: 450}, {2: 2849}, {1: 3150}, {2: 5999}],
                        [{1: 0}, {2: 749}, {1: 1050}, {2: 3449}, {1: 5250}, {2: 5999}],
                        [{1: 0}, {2: 3899}, {1: 4500}, {2: 4799}]
                        ]
        chunk_duration (float): The duration of each chunk in seconds.
        
    Returns:
        list: The same list structure with each numeric timestamp adjusted by the cumulative offset.
    """
    cumulative_offset = 0
    adjusted_chunks = []
    
    for chunk in chunks:
        adjusted_chunk = []
        for item in chunk:
            adjusted_item = {}
            for key, value in item.items():
                # Adjust only if the value is not None.
                adjusted_item[key] = value + cumulative_offset if value is not None else None
            adjusted_chunk.append(adjusted_item)
        adjusted_chunks.append(adjusted_chunk)
        
        # Increase the cumulative offset by the chunk duration in milliseconds.
        # PF-патч: сдвигаем ТОЛЬКО для непустых чанков — как в
        # adjust_all_chunks_cumulative (`if chunk:`). Пустые чанки (сигнальные
        # PARTIAL/FULL: тишина + пустые vad_marks) НЕ сдвигают ни сегментный,
        # ни mark-таймлайн → они остаются согласованными и
        # check_and_adjust_interval клипит сегменты по истинным границам аудио.
        # Для production-трафика (каждый чанк с непустыми marks) поведение
        # не меняется.
        if chunk:
            cumulative_offset += chunk_duration * 1000
    
    return process_vad_results(adjusted_chunks)


def merge_speaker_segments(segments, max_gap=1500):
    """
    Merge speaker segments if the gap between segments is less than or equal to max_gap.

    Args:
        segments (list): List of segments in the format [start, end, speaker_id].
        max_gap (int): Maximum allowed gap in milliseconds to merge segments.

    Returns:
        list: Merged speaker segments.
    """
    if not segments:
        return []

    # Sort segments by start time to ensure proper order
    segments.sort(key=lambda x: x[0])

    merged_segments = [segments[0]]

    for current in segments[1:]:
        previous = merged_segments[-1]

        # Check if current segment belongs to the same speaker and within max_gap
        if current[2] == previous[2] and current[0] - previous[1] <= max_gap:
            # Merge segments by extending the end time
            previous[1] = current[1]
        else:
            merged_segments.append(current)

    return merged_segments


def vad_formatting(inp_segs):
    out_segs = []
    for tensor in inp_segs:
        start, end = tensor.tolist()
        out_segs.append({'start': start, 'end': end})
    return out_segs


def read_labels(subseg_ids, label, frame_shift=10):
    utt_to_subseg_labels = []
    new_sort = {}
    for i, subseg in enumerate(subseg_ids):
        (utt, seg_idx, begin_ms, end_ms, begin_frames, end_frames) = subseg
        begin = (int(begin_ms * 1000) +
                    int(begin_frames) * frame_shift) / 1000.0
        end = (int(begin_ms * 1000) +
                int(end_frames) * frame_shift) / 1000.0
        new_sort[seg_idx] = (begin, end, label[i])
    utt_to_subseg_labels = list(dict(sorted(new_sort.items())).values())
    return utt_to_subseg_labels


def merge_segments(subseg_to_labels):
    merged_segment_to_labels = []

    if len(subseg_to_labels) == 0:
        return merged_segment_to_labels

    if len(subseg_to_labels) == 1:
        return subseg_to_labels

    (begin, end, label) = subseg_to_labels[0]
    for (b, e, la) in subseg_to_labels[1:]:
        if b <= end and la == label:
            end = e
        elif b > end:
            merged_segment_to_labels.append((begin, end, label))
            begin, end, label = b, e, la
        elif b <= end and la != label:
            pivot = (b + end) / 2.0
            merged_segment_to_labels.append((begin, pivot, label))
            begin, end, label = pivot, e, la
        else:
            raise ValueError
    merged_segment_to_labels.append((begin, e, label))

    return merged_segment_to_labels


def convert_to_vad_mark(sample, mark):

    vad_mark = output_pb2.VoiceActivityMark()
    vad_mark.mark_type = mark
    offset_ms = int(sample * 1000)
    vad_mark.offset_ms = offset_ms
    return vad_mark

def check_and_adjust_interval(audiogram_vad_marks, interval):
    begin_ms, end_ms = interval

    # Функция для проверки и корректировки одного значения
    def adjust_time(ms, marks):
        for start, end in marks:
            if start <= ms <= end:
                return ms  # Значение ms уже в пределах допустимого интервала
        # Найдем ближайший интервал
        closest_start = closest_end = None
        for start, end in marks:
            if ms < start:
                closest_start = start
                break
            closest_end = end

        # Корректируем значение: выбираем ближайшую границу
        if closest_start is not None and closest_end is not None:
            if abs(ms - closest_end) < abs(ms - closest_start):
                return closest_end
            else:
                return closest_start
        elif closest_start is not None:
            return closest_start
        elif closest_end is not None:
            return closest_end

    return [adjust_time(begin_ms, audiogram_vad_marks), adjust_time(end_ms, audiogram_vad_marks)]


def split_intervals(interval, max_interval):
    start, end = interval
    intervals = []
    current_start = start
    while current_start < end:
        current_end = min(current_start + max_interval, end)
        intervals.append((current_start, current_end))
        current_start = current_end + 1
    
    return intervals


def split_intervals_with_speaker(intervals, max_interval):
    """
    Split every [start, end, speaker_id] interval in `intervals`
    into smaller chunks whose length is at most `max_interval`,
    carrying the speaker_id through to every chunk.

    Parameters
    ----------
    intervals : list[list[int, int, int]]
        e.g. [[150, 81299, 1], [82950, 507599, 1], ...]
    max_interval : int
        Maximum length (in the same units as start / end).

    Returns
    -------
    list[list[int, int, int]]
        The flattened list of split intervals.
    """
    result = []
    for start, end, speaker_id in intervals:
        current_start = start
        while current_start < end:
            current_end = min(current_start + max_interval, end)
            result.append([current_start, current_end, speaker_id])
            current_start = current_end + 1   # keep intervals non-overlapping
    return result


def preprocess_vad_results_сurrent(vad_results):
    """
    Process streaming VAD results into a standard list of dictionaries with keys:
      - 'start': the segment start time in seconds.
      - 'end': the segment end time in seconds.

    Timestamps in the input (milliseconds) are converted to seconds in the output.
    
    The function handles edge conditions where zeros (key 0) occur only at the beginning or
    at the end of a chunk:
      1. Leading zeros at the beginning of a chunk indicate an ongoing segment from previous chunks.
      2. Trailing zeros at the end of a chunk (after a begin marker) update the end timestamp.

    Each token is a dict with one key:
      - 0: "None" marker.
      - 1: Begin marker.
      - 2: End marker.
    
    Returns:
        A list of dictionaries in the format:
        [{'start': <seconds>, 'end': <seconds>}, ...]
    """
    segments = []
    open_segment = None  # Represents an ongoing segment as a tuple (start, last_value)

    # Process one chunk at a time.
    for chunk in vad_results:
        if not chunk:
            continue  # Skip empty chunks

        # Find indices of explicit markers (keys 1 and 2) in the chunk.
        explicit_indices = [i for i, token in enumerate(chunk) if list(token.keys())[0] in (1, 2)]
        
        if explicit_indices:
            first_explicit = explicit_indices[0]
            last_explicit = explicit_indices[-1]
            
            # --- Process leading tokens (edge zeros before the first explicit marker) ---
            if first_explicit > 0:
                # All tokens before the first explicit marker should be zeros.
                # Use the first token’s value as the start and the last token in this block as the current "end".
                first_token = chunk[0]
                last_leading_token = chunk[first_explicit - 1]
                leading_start = list(first_token.values())[0]
                leading_end = list(last_leading_token.values())[0]
                if open_segment is None:
                    open_segment = (leading_start, leading_end)
                else:
                    # Extend the existing open segment with the trailing value from the leading block.
                    open_segment = (open_segment[0], leading_end)
            
            # --- Process explicit tokens (the middle part) ---
            for i in explicit_indices:
                token = chunk[i]
                key = list(token.keys())[0]
                value = token[key]
                if key == 1:
                    # Begin marker encountered.
                    if open_segment is not None:
                        # If there is an open segment (possibly extended by edge zeros), close it.
                        segments.append({'start': open_segment[0] / 1000.0, 'end': open_segment[1] / 1000.0})
                    # Start a new segment.
                    open_segment = (value, value)
                elif key == 2:
                    # End marker encountered.
                    if open_segment is not None:
                        segments.append({'start': open_segment[0] / 1000.0, 'end': value / 1000.0})
                        open_segment = None
                    # If no open segment exists, ignore this token.
            
            # --- Process trailing tokens (tokens after the last explicit marker) ---
            if last_explicit < len(chunk) - 1:
                # These tokens should all be zeros.
                trailing_token = chunk[-1]
                trailing_value = list(trailing_token.values())[0]
                # Only if the segment is still open (e.g. last explicit was a begin marker)
                if open_segment is not None:
                    open_segment = (open_segment[0], trailing_value)
                # If open_segment is None (meaning the segment was already closed), ignore trailing zeros.
        else:
            # --- Chunk has only zeros ---
            # In this case update any existing open segment.
            if open_segment is not None:
                trailing_value = list(chunk[-1].values())[0]
                open_segment = (open_segment[0], trailing_value)
            # If there is no open segment, ignore the chunk.
    
    # After processing all chunks, if a segment remains open,
    # output it as a final speech segment.
    if open_segment is not None:
        segments.append({'start': open_segment[0] / 1000.0, 'end': open_segment[1] / 1000.0})
    
    return [segments]


def convert_to_seconds(time_string: str) -> tuple[float, float]:
    begin = time_string.split(' ')[1].split(' ')[0]
    hours, minutes, seconds = map(float, begin.split(':'))
    begin = hours * 3600 + minutes * 60 + seconds

    end = time_string.split('  ')[1][:-1]
    hours, minutes, seconds = map(float, end.split(':'))
    end = hours * 3600 + minutes * 60 + seconds    
    return begin, end


class PyannetState:
    
    def __init__(self):
        # self.aggregated[i] will be used to store the sum of all predictions
        self.aggregated = np.zeros((0,), dtype=np.float32)
        # self.overlapping[i] will be used to store the number of chunks
        # that contributed to frame #i
        self.overlapping = np.zeros((0,), dtype=np.float32)
        self.aggregated_len = 0
        self.n_windows = 0
        self.leftover = np.zeros((1, 0), dtype=np.float32)
        self.start_bin = 0
        self.is_active = False
        self.annotation = None
        self.time = 0

class PyannetProcessing:
    
    def __init__(self):
        self.onset = 0.3
        self.offset = 0.3
        self.min_duration_on = 0.0
        self.min_duration_off = 0.0

        self._frame_duration = 0.016875
        self._model_duration = 5.0
        self._model_step = 0.5
        
        sample_rate = 16000
        self._limit = int(30.0 * sample_rate)
        self._model_duration_samples = int(self._model_duration * sample_rate)
        self._model_step_samples = int(self._model_step * sample_rate)
        
        self._out_window_size = 293
        # Hamming window used for overlap-add aggregation
        self._hamming_window = np.hamming(self._out_window_size).astype(np.float32)
    
    async def __call__(
        self,
        audio: np.ndarray,
        is_last: bool,
        state: PyannetState | None = None,
        end_timestamp: float | None = None,
    ) -> tuple[list[np.ndarray], PyannetState]:
        if state is None:
            state = PyannetState()
        
        start_audio = 0
        audio = np.append(state.leftover, audio, axis=1)
        length = audio.shape[1]
        awaitables = []
        while True:
            length = min(length, self._limit)
            
            if length <= 0:
                break
            
            if not is_last and length < self._model_duration_samples:
                state.leftover = audio[:, start_audio:]
                break

            step = (
                (length - self._model_duration_samples) // self._model_step_samples + 1
            ) * self._model_step_samples

            if not is_last:
                length = step - self._model_step_samples + self._model_duration_samples
            elif audio.shape[1] - start_audio <= self._limit:
                step = length

            inference_request = pb_utils.InferenceRequest(
                model_name="pyannet",
                requested_output_names=["scores", "out_lens"],
                inputs=[pb_utils.Tensor("waveforms", audio[:, start_audio : start_audio + length])],
                preferred_memory=pb_utils.PreferredMemory(pb_utils.TRITONSERVER_MEMORY_CPU),
            )
            awaitables.append(inference_request.async_exec())
            start_audio += step
            length = audio.shape[1] - start_audio
        
        inference_responses = []
        if awaitables:
            inference_responses = await asyncio.gather(*awaitables)
        
        for i, inference_response in enumerate(inference_responses):
            if inference_response.has_error():
                raise pb_utils.TritonModelException(
                    inference_response.error().message())

            data = pb_utils.get_output_tensor_by_name(inference_response, "scores").as_numpy()[0]
            lens = pb_utils.get_output_tensor_by_name(inference_response, 'out_lens').as_numpy()[0]
            data = [
                data[k, :int(lens[k]), 0] for k in range(data.shape[0]) if int(lens[k]) > 0
            ]

            for j, scores in enumerate(data):              
                hamming_window = self._hamming_window[:len(scores)]
                start_frame = int(round((state.n_windows * self._model_step - .5 * self._frame_duration) / self._frame_duration))
                state.n_windows += 1
                pad = len(scores) - len(state.aggregated)
                state.aggregated_len += pad
                state.aggregated = np.pad(state.aggregated, (0, pad))
                state.overlapping = np.pad(state.overlapping, (0, pad))
                state.aggregated += scores * hamming_window
                state.overlapping += hamming_window
                
                end_frame = int(round((state.n_windows * self._model_step - .5 * self._frame_duration) / self._frame_duration)) - start_frame
                
                if is_last and i == len(inference_responses) - 1 and j == len(data) - 1:
                    num_frames = int(round(
                        (self._model_duration + (state.n_windows - 1) * self._model_step - .5 * self._frame_duration) / self._frame_duration
                    )) + 1 - self._out_window_size + len(scores)
                    pad = num_frames - state.aggregated_len
                    if pad > 0:
                        state.aggregated = np.pad(state.aggregated, (0, pad))
                        state.overlapping = np.pad(state.overlapping, (0, pad))
                    end_frame = len(state.aggregated)
                
                aggregated = state.aggregated[:end_frame] / np.maximum(state.overlapping[:end_frame], 1e-12)
                state.aggregated = state.aggregated[end_frame:]
                state.overlapping = state.overlapping[end_frame:]
                for score in aggregated:
                    self._binarize(score, state)
        
        if state.annotation is None:
            return [], state

        # if annotation at the end, add final region
        if is_last and state.is_active:
            region = Segment(state.start_bin, end_timestamp if end_timestamp is not None else state.time)
            state.annotation[region, 0] = 0
        
        # because of padding, some annotation regions might be overlapping: merge them.
        # also: fill same speaker gaps shorter than min_duration_off
        if self.min_duration_off > 0.0:
            state.annotation = state.annotation.support(collar=self.min_duration_off)
        
        segments = []
        timeline = state.annotation.get_timeline(copy=True)
        for i, region in enumerate(timeline):
            if self.min_duration_on > 0 and region.duration < self.min_duration_on:
                del state.annotation[region, 0]
                continue
            
            if not is_last and self.min_duration_off > 0 and \
               i == len(timeline) - 1 and \
               state.time - region.end < self.min_duration_off:
                continue

            segments.append(
                np.array(list(convert_to_seconds(str(region))), dtype=np.float32)
            )
            del state.annotation[region, 0]
        
        return segments, state

    def _binarize(self, score: float, state: PyannetState) -> None:        
        if state.annotation is None:
            state.annotation = Annotation()
            state.start_bin = self._frame_duration / 2.
            state.time = state.start_bin
            state.is_active = score > self.onset
            return
        
        state.time += self._frame_duration

        # currently annotation
        if state.is_active:
            # switching from annotation to inactive
            if score < self.offset:
                region = Segment(state.start_bin, state.time)
                state.annotation[region, 0] = 0
                state.start_bin = state.time
                state.is_active = False

        # currently inactive
        else:
            # switching from inactive to annotation
            if score > self.onset:
                state.is_active = True
                if self.min_duration_off > 0 and len(state.annotation) > 0:
                    timeline = state.annotation.get_timeline(copy=True)
                    if state.time - timeline[-1].end < self.min_duration_off:
                        del state.annotation[timeline[-1], 0]
                        state.start_bin = timeline[-1].start
                        return
                state.start_bin = state.time
