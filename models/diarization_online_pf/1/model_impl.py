import triton_python_backend_utils as pb_utils
from torch.utils.dlpack import to_dlpack, from_dlpack
import torch
import numpy as np
import json
import asyncio
import time
import logging
import threading
import utils
import os
from diarization_protocol import output_pb2

logging.basicConfig(
    #filename='embedder_logs.logs',
    #filemode='a',
    format="%(asctime)s,%(msecs)d %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

MARK_NONE = 0
MARK_BEGIN = 1
MARK_END = 2
ASR_MAX = 30000 # ASR MAX CAPABLE INPUT SIZE IN MS

# PF-патч: partial finalization с сохранением состояния + стабильные speaker ID.
# PF:PARTIAL — вернуть полный накопленный снапшот, состояние НЕ освобождается.
# PF:FULL (sequence_end) — финальный результат, состояние освобождается.
PF_MATCH_THRESHOLD = 0.75  # мин. косинус для перематча raw-кластера на сохранённый reference
PF_REF_ALPHA = 0.5         # скользящее среднее при обновлении reference-центроида


def pf_normalize(vec):
    """L2-нормализация вектора (нулевой вектор возвращается как есть)."""
    nrm = np.linalg.norm(vec)
    if nrm < 1e-9:
        return vec
    return vec / nrm


def pf_stable_speaker_mapping(raw_centroids, refs,
                              threshold=PF_MATCH_THRESHOLD, alpha=PF_REF_ALPHA):
    """
    Глобальный жадный попарный маппинг raw-кластеров на сохранённые reference-центроиды.

    Args:
        raw_centroids: dict raw_label(int) -> нормализованный центроид (ndarray 256-d)
        refs:          dict stable_id(int) -> нормализованный reference-центроид (ndarray 256-d)

    Returns:
        (stable_map, refs_updated, matches, new_ids)
        stable_map:   dict raw_label -> stable_id (все raw-кластеры получают id)
        refs_updated: обновлённые references (скользящее среднее по совпавшим парам)
        matches:      list (raw_label, stable_id, cos) — перематчи
        new_ids:      list (raw_label, new_stable_id) — новые speakers
    """
    refs_updated = {sid: np.array(ref, copy=True) for sid, ref in refs.items()}
    stable_map = {}
    matches = []
    new_ids = []

    # Все пары (raw, ref) с косинусом, отсортированы по убыванию — глобальная жадность
    pairs = []
    for raw, c in raw_centroids.items():
        c_n = pf_normalize(np.asarray(c, dtype=np.float64))
        if np.linalg.norm(c_n) < 1e-9:
            continue
        for sid, r in refs.items():
            r_n = pf_normalize(np.asarray(r, dtype=np.float64))
            if np.linalg.norm(r_n) < 1e-9:
                continue
            cos = float(np.dot(c_n, r_n))
            if cos >= threshold:
                pairs.append((cos, int(raw), int(sid)))
    pairs.sort(key=lambda t: t[0], reverse=True)

    used_raw = set()
    used_ref = set()
    for cos, raw, sid in pairs:
        if raw in used_raw or sid in used_ref:
            continue
        stable_map[raw] = sid
        used_raw.add(raw)
        used_ref.add(sid)
        matches.append((raw, sid, cos))

    # Несовпавшие raw-кластеры получают новые id (max+1, по возрастанию raw-метки — детерминированно);
    # их центроиды сохраняются как новые references
    next_id = (max(refs.keys()) + 1) if refs else 1
    for raw in sorted(raw_centroids.keys()):
        if raw in stable_map:
            continue
        stable_map[int(raw)] = next_id
        new_ids.append((int(raw), next_id))
        refs_updated[next_id] = pf_normalize(np.asarray(raw_centroids[raw], dtype=np.float64))
        next_id += 1

    # Обновление references скользящим средним по совпавшим парам
    for raw, sid, cos in matches:
        c_n = pf_normalize(np.asarray(raw_centroids[raw], dtype=np.float64))
        r_n = pf_normalize(np.asarray(refs_updated[sid], dtype=np.float64))
        refs_updated[sid] = pf_normalize(alpha * c_n + (1.0 - alpha) * r_n)

    return stable_map, refs_updated, matches, new_ids

try:
    VAD_TYPE = int(os.environ.get('DIARIZATION_VAD_TYPE', '2'))
except:
    VAD_TYPE = 1
    logging.info('Incorrect VAD type was defined in ENV. Switching to 1')
# VAD_TYPE = 0
# 0 - use audiogram vad
# 1 - use webrtc vad
# 2 - use pyannote vad



class TritonPythonModel:
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    def initialize(self, args):
        """`initialize` is called only once when the model is being loaded.
        Implementing `initialize` function is optional. This function allows
        the model to initialize any state associated with this model.
        Parameters
        ----------
        args : dict
          Both keys and values are strings. The dictionary keys and values are:
          * model_config: A JSON string containing the model configuration
          * model_instance_kind: A string containing model instance kind
          * model_instance_device_id: A string containing model instance
            device ID
          * model_repository: Model repository path
          * model_version: Model version
          * model_name: Model name
        """
     
        # Get OUTPUT0 configuration
        self.model_config = json.loads(args['model_config'])
        if "GPU" in self.model_config["instance_group"][0]["kind"]:
            self.device = "cuda"
        else:
            self.device = "cpu"

        self.sample_rate = int(self.model_config['parameters']['sample_rate']['string_value'])
        self.window_fs = float(self.model_config['parameters']['window_fs']['string_value'])
        self.period_fs = float(self.model_config['parameters']['period_fs']['string_value'])
        self.frame_shift = int(self.model_config['parameters']['frame_shift']['string_value'])
        self.max_speaker = int(self.model_config['parameters']['max_speaker']['string_value'])
        self.num_speaker = int(self.model_config['parameters']['num_speaker']['string_value'])

        self.use_vad = self.model_config['parameters']['use_audiogram_vad']['string_value'].lower() == 'true'

        output0_config = pb_utils.get_output_config_by_name(self.model_config, "DiarisationResponse")
        # Convert Triton types to numpy types
        self.output0_dtype = pb_utils.triton_string_to_numpy(output0_config['data_type'])
        
        self.sequence_id_embeddings = {} # dict for saving embeddings results for every sequence chunks
        self.sequence_id_vad = {} # dict for saving vad results for every sequence chunks
        #self.sequence_id_vad_audiogram = {} # dict for saving audiogram vad results for every sequence chunks
        self.sequence_id_vad_mark_audiogram = {} # dict for saving audiogram vad results for every sequence chunks

        # PF-патч: corrid -> {stable_id: нормализованный reference-центроид 256-d}
        self.sequence_id_speaker_refs = {}

        self.sequence_id_last_update = {}
        
        self.sequence_id_pyannet_state = {}  # dict for saving pyannet state for every sequence chunks
        self.sequence_id_wav = {}  # dict for saving wav for every sequence chunks
        
        self.processing = utils.PyannetProcessing()
        
        self.true_online = False

        # Launch cleanup in a separate thread
        cleanup_thread = threading.Thread(target=self.cleanup_inactive_sequences, args=(600,), daemon=True)
        cleanup_thread.start()

    def cleanup_inactive_sequences(self, timeout=600):
        while True:
            current_time = time.time()
            inactive_sequences = [seq_id for seq_id, last_time in self.sequence_id_last_update.items()
                                if current_time - last_time > timeout]
            if len(inactive_sequences) > 0:
                logging.info(f"Cleaned inactive sequences data: {inactive_sequences}")
            for seq_id in inactive_sequences:
                if seq_id in self.sequence_id_vad:
                    del self.sequence_id_vad[seq_id]

                # if seq_id in self.sequence_id_vad_audiogram:
                #     del self.sequence_id_vad_audiogram[seq_id]
                
                if seq_id in self.sequence_id_vad_mark_audiogram:
                    del self.sequence_id_vad_mark_audiogram[seq_id]

                if seq_id in self.sequence_id_embeddings:
                    del self.sequence_id_embeddings[seq_id]

                # PF-патч: вместе с состоянием удаляем и speaker references
                if seq_id in self.sequence_id_speaker_refs:
                    del self.sequence_id_speaker_refs[seq_id]

                del self.sequence_id_last_update[seq_id]

                if self.true_online and seq_id in self.sequence_id_pyannet_state:
                    del self.sequence_id_pyannet_state[seq_id]
                
                if self.true_online and seq_id in self.sequence_id_wav:
                    del self.sequence_id_wav[seq_id]

            time.sleep(60)
        
    async def call_vad(self,vad_name, wav, end_t):
        inference_response_awaits1 = []
        input_tensor_spk0 = pb_utils.Tensor.from_dlpack("input", to_dlpack(wav))
        
        end_segment_data = np.array([[end_t]], dtype=np.float32)
        sample_rate_data = np.array([[self.sample_rate]], dtype=np.float32)
        input_tensor_end_segment = pb_utils.Tensor("end_segment", end_segment_data)
        input_tensor_sample_rate = pb_utils.Tensor("sample_rate", sample_rate_data)

        input_tensors_spk = [input_tensor_spk0, input_tensor_end_segment, input_tensor_sample_rate]

        inference_request = pb_utils.InferenceRequest(model_name=vad_name, requested_output_names=['LABELS'], inputs=input_tensors_spk)
        inference_response_awaits1.append(inference_request.async_exec())
        inference_responses = await asyncio.gather(*inference_response_awaits1)
        total_vads = []
        for inference_response in inference_responses:
            if inference_response.has_error():
                raise pb_utils.TritonModelException(
                    inference_response.error().message())
            else:
                batched_result_vad = pb_utils.get_output_tensor_by_name(
                    inference_response, 'LABELS')
                total_vads.extend(from_dlpack(batched_result_vad.to_dlpack()))
        return total_vads
    
    async def call_pyannet(self, wav, end_t, is_last, corrid):
        wav_float = wav.float().numpy()
        
        state = utils.PyannetState()
        if self.true_online:
            state = self.sequence_id_pyannet_state[corrid]

        segments, state = await self.processing(
            audio=wav_float,
            is_last=is_last if self.true_online else True,
            state=state,
            end_timestamp=end_t,
        )

        if self.true_online:
            self.sequence_id_pyannet_state[corrid] = state
        
        return segments

    def subsegment(self, wav, segments, wav_idx, window_fs: float = 1.52, period_fs: float = 0.7,
                   sr: int = 16000, frame_shift: int = 10):

        def repeat_to_fill(x, window_fs):
            length = x.size(0)
            num = (window_fs + length - 1) // length
            x = x.repeat(1, num)[0][:window_fs]
            input = torch.zeros((1, window_fs), device=self.device)
            input[0] = x
            return input

        subsegs = []
        subseg_signals = []

        seg_idx = 0

        window_fs_sample = int(window_fs * sr)
        period_fs_sample = int(period_fs * sr)
        for segment in segments:
            seg_begin = int(segment['start'] * sr)
            seg_end = int(segment['end'] * sr)
            seg_signal = wav[seg_begin:seg_end + 1]
            seg_length = seg_end - seg_begin

            if seg_length <= window_fs_sample:
                subseg = [
                    wav_idx, seg_idx, segment['start'], segment['end'], 0,
                    int(seg_length / sr * 1000 // frame_shift)
                ]
                subseg_signal = repeat_to_fill(seg_signal, window_fs_sample)

                subsegs.append(subseg)
                subseg_signals.append(subseg_signal)
                seg_idx += 1
            else:
                max_subseg_begin = int(seg_length - window_fs_sample + period_fs_sample)

                for subseg_begin in range(0, max_subseg_begin, period_fs_sample):
                    subseg_end = min(subseg_begin + window_fs_sample, seg_length)
                    subseg = [
                        wav_idx, seg_idx, segment['start'], segment['end'],
                        int(subseg_begin / sr * 1000 / frame_shift),
                        int(subseg_end / sr * 1000 / frame_shift)
                    ]
                    subseg_signal = repeat_to_fill(
                        seg_signal[subseg_begin:subseg_end + 1], window_fs_sample   )

                    subsegs.append(subseg)
                    subseg_signals.append(subseg_signal)
                    seg_idx += 1

        return subsegs, subseg_signals
     
    async def execute(self, requests):
        """`execute` must be implemented in every Python model. `execute`
        function receives a list of pb_utils.InferenceRequest as the only
        argument. This function is called when an inference is requested
        for this model.
        Parameters
        ----------
        requests : list
          A list of pb_utils.InferenceRequest
        Returns
        -------
        list
          A list of pb_utils.InferenceResponse. The length of this list must
          be the same as `requests`
        """
        vad_start_time = time.time()
        batch_count = []
        batch_len = []

        total_wavs = []
        responses = []
        
        vad_response_awaits = []

        sr = self.sample_rate
        window_fs = self.window_fs
        period_fs = self.period_fs
        frame_shift = self.frame_shift
        max_speaker = self.max_speaker
        num_speaker = self.num_speaker

        max_speaker_b = []
        num_speaker_b = []
        
        vad_awaitables = []
        out_segs = []

        for request in requests:
            sender = request.get_response_sender()
            params_str = request.parameters()
            params = json.loads(params_str)

            window_fs = float(params.get("window_fs", f"{self.window_fs}"))
            period_fs = float(params.get("period_fs", f"{self.period_fs}"))
            frame_shift = int(params.get("frame_shift", f"{self.frame_shift}"))
            sample_rate = int(params.get("sample_rate", f"{self.sample_rate}"))
            max_speaker = int(params.get("max_speaker", f"{self.max_speaker}"))
            num_speaker = int(params.get("num_speaker", f"{self.num_speaker}"))

            max_speaker_b.append([max_speaker])
            num_speaker_b.append([num_speaker])
            # Switched from config sequence control to the code
            start_signal = bool(request.flags() & pb_utils.TRITONSERVER_REQUEST_FLAG_SEQUENCE_START)
            end_signal = bool(request.flags() & pb_utils.TRITONSERVER_REQUEST_FLAG_SEQUENCE_END)
            corrid = request.correlation_id()

            # PF-патч: partial finalization — вернуть накопленный снапшот, сохранив состояние
            partial_finalization = str(params.get("partial_finalization", "False")).strip().lower() == "true"
            do_finalize = end_signal or (partial_finalization and not end_signal)

            if start_signal == True:
                self.sequence_id_embeddings[corrid] = {}
                self.sequence_id_vad[corrid] = {}
                #self.sequence_id_vad_audiogram[corrid] = {}
                self.sequence_id_vad_mark_audiogram[corrid] = {}
                # PF-патч: reference-центроиды стабильных speaker ID для sequence
                self.sequence_id_speaker_refs[corrid] = {}
                if self.true_online:
                    self.sequence_id_pyannet_state[corrid] = utils.PyannetState()
                    self.sequence_id_wav[corrid] = torch.zeros((0,), dtype=torch.float16, device=self.device)


            input0 = pb_utils.get_input_tensor_by_name(request, "input")
            chunk_id_tensor = pb_utils.get_input_tensor_by_name(request, "chunk_id")
            chunk_id = chunk_id_tensor.as_numpy().astype(np.uint16)[0][0]
            vad_marks = pb_utils.get_input_tensor_by_name(request, "vad_marks").as_numpy()
            vad_marks = output_pb2.SpeakerSegment.FromString(vad_marks[0][0])
            cur_b_wav = from_dlpack(input0.to_dlpack())
            # Initialize zero tensor with the same shape
            masked_tensor = torch.zeros_like(cur_b_wav[0])
            audiogram_vad_final = []
            for mark in vad_marks.va_marks:
                mark_dict = dict()
                mark_dict[mark.mark_type ] = mark.offset_ms
                audiogram_vad_final.append(mark_dict)
            # File masking, don't use right now
            masked_tensor[0:len(cur_b_wav[0])] = cur_b_wav[0][0:len(cur_b_wav[0])] # if we don't use vad, then define vad results is file_start, file_end
            end_timestamp = len(cur_b_wav[0])/self.sample_rate  # Here we calculate in raw
            if self.true_online:
                end_timestamp += len(self.sequence_id_wav[corrid])/self.sample_rate

            cur_b_wav[0] = masked_tensor
            
            if VAD_TYPE == 0:
                logging.info('using audiogramm')
                out_segs.append(utils.preprocess_vad_results_сurrent([audiogram_vad_final]))

            if VAD_TYPE == 1:
                logging.info('using webrtc')
                vad_awaitables.append(self.call_vad('vad',cur_b_wav, end_timestamp))

            if VAD_TYPE == 2:
                logging.info('using pyannote')
                vad_awaitables.append(self.call_pyannet(cur_b_wav, end_timestamp, end_signal, corrid))
                            
            cur_batch = cur_b_wav.shape[0]
            cur_len = cur_b_wav.shape[1]
            cur_len_sec = cur_len/self.sample_rate

            batch_count.append(cur_batch)
            batch_len.append(cur_len)

            for wav in cur_b_wav:
                wav = wav.to(device=self.device, dtype=torch.float16)
                if self.true_online:
                    self.sequence_id_wav[corrid] = torch.cat((self.sequence_id_wav[corrid], wav),  dim=0)
                    wav = self.sequence_id_wav[corrid]
                total_wavs.append(wav)
        
        if VAD_TYPE in (1, 2):
            out_segs = [utils.vad_formatting(vad_res) for vad_res in await asyncio.gather(*vad_awaitables)]

        speech_shapes = []
        all_probs = []
        
        total_subsegments = []
        total_subsegment_ids = []
        total_embds = []

        wav_idx = 0
        for waveform, segments in zip(total_wavs, out_segs):
            subsegs, subseg_signals = self.subsegment(waveform, segments, wav_idx, window_fs, period_fs, sr, frame_shift)
            total_subsegments.extend(subseg_signals)
            total_subsegment_ids.extend(subsegs)
            wav_idx += 1

        vad_end_time = time.time()
        inf_time = round(vad_end_time-vad_start_time, 3) or 0.01
        logging.info(f"1:VAD               | Process {corrid} sequence {chunk_id} chunk {round(cur_len_sec, 2)} sec. in {inf_time} sec. RTF: {round(cur_len_sec/inf_time)}")
            
        emb_start_time = time.time()
        
        inference_response_awaits = []
        for wavs in total_subsegments:
            input_tensor_spk0 = pb_utils.Tensor.from_dlpack(
                "waveform", to_dlpack(wavs))
            input_tensors_spk = [input_tensor_spk0]
            inference_request = pb_utils.InferenceRequest(
                model_name='embedder',
                requested_output_names=['embs'],
                inputs=input_tensors_spk)
            inference_response_awaits.append(inference_request.async_exec())
        inference_responses = await asyncio.gather(*inference_response_awaits)
        for inference_response in inference_responses:
            if inference_response.has_error():
                raise pb_utils.TritonModelException(
                    inference_response.error().message())
            else:
                batched_result = pb_utils.get_output_tensor_by_name(
                    inference_response, 'embs')
                total_embds.extend(from_dlpack(batched_result.to_dlpack()))
                
        out_embds = list()
        out_time_info = list()
        for i in range(0, len(total_wavs)):
            out_embds.append(list())
            out_time_info.append(list())

        for subseg_idx, embds in zip(total_subsegment_ids, total_embds):
            wav_idx = subseg_idx[0]
            out_embds[wav_idx].append(embds)
            out_time_info[wav_idx].append(subseg_idx)
        
        self.sequence_id_vad[corrid][chunk_id] = out_time_info[0]
        #self.sequence_id_vad_audiogram[corrid][chunk_id] = audiogram_vad_marks
        self.sequence_id_vad_mark_audiogram[corrid][chunk_id] = audiogram_vad_final

        self.sequence_id_last_update[corrid] = time.time()

        if do_finalize:
            sorted_chunks = sorted(self.sequence_id_vad[corrid].keys())
            # Create a list of the values in sorted order
            all_chunks = [self.sequence_id_vad[corrid][cid] for cid in sorted_chunks]
            chunk_len = int(len(cur_b_wav[0])/self.sample_rate)
            out_time_info[0] = utils.adjust_all_chunks_cumulative(
                all_chunks,
                chunk_duration=chunk_len if not self.true_online else None
            )

            sorted_chunks = sorted(self.sequence_id_vad_mark_audiogram[corrid].keys())
            # Create a list of the values in sorted order
            all_chunks = [self.sequence_id_vad_mark_audiogram[corrid][cid] for cid in sorted_chunks]
            audiogram_vad_marks = utils.adjust_audiogram_marks(all_chunks, chunk_len)

        emb_end_time = time.time()

        inf_time = round(emb_end_time - emb_start_time, 3) or 0.01
        
        logging.info(f"2:Feature Extractor | Process {corrid} sequence {chunk_id} chunk {round(cur_len_sec, 2)} sec. in {inf_time} sec. RTF: {round(cur_len_sec/inf_time)}")

        start_cls_time = time.time()
        # Begin clustering
        flag_bug = False
        inference_response_awaits = []

        self.sequence_id_embeddings[corrid][chunk_id] = out_embds[0]
        self.sequence_id_last_update[corrid] = time.time()

        response_sender = request.get_response_sender()

        if do_finalize:
            flattened_list = []
            # Sort the chunk_ids to maintain order (assumes chunk_id keys can be sorted numerically or lexically)
            for cid in sorted(self.sequence_id_embeddings[corrid].keys()):
                    flattened_list.extend(self.sequence_id_embeddings[corrid][cid])
            out_embds = [flattened_list]

            for i, embd in enumerate(out_embds):
                if embd:  # Check if embd is not empty
                    embd = torch.stack(embd) ### Check
                    if embd.shape == (1, 256):
                        flag_bug= True
                        embd= torch.zeros(2, 256)

                    input_tensor_embds0 = pb_utils.Tensor.from_dlpack(
                        "EMBEDDINGS", to_dlpack(torch.unsqueeze(embd, 0)))


                    num_speaker = np.array(num_speaker_b, dtype=np.float32)
                    input_tensor_num_speaker = pb_utils.Tensor.from_dlpack(
                        "NUM_SPEAKER", to_dlpack(torch.tensor(num_speaker))
                    )

                    max_speaker = np.array(max_speaker_b, dtype=np.float32)
                    input_tensor_max_speaker = pb_utils.Tensor.from_dlpack(
                        "MAX_SPEAKER", to_dlpack(torch.tensor(max_speaker))
                    )
                    ## FIX
                    input_tensors_spk = [input_tensor_embds0, input_tensor_num_speaker, input_tensor_max_speaker]
                    inference_request = pb_utils.InferenceRequest(
                        model_name='clusterer',
                        requested_output_names=['LABELS'],
                        request_id=str(i),
                        inputs=input_tensors_spk)
                    inference_response_awaits.append(inference_request.async_exec())
                else:
                # Handle the case where embd is empty
                    logging.warning(f"Empty embeddings for request {i}")
                
            inference_responses = await asyncio.gather(*inference_response_awaits)

            i = 0
            results = []
            for inference_response in inference_responses:
                if inference_response.has_error():
                    raise pb_utils.TritonModelException(
                        inference_response.error().message())
                else:
                    result = pb_utils.get_output_tensor_by_name(inference_response, 'LABELS').as_numpy()[0]
                    if flag_bug == True:
                        result = [0]
                    utt_to_subseg_labels = utils.read_labels(out_time_info[i], result)
                    i += 1
                    rttm = utils.merge_segments(utt_to_subseg_labels)
                    if len(rttm) > 0:
                        results.append(rttm)

            # PF-патч: стабильные speaker ID.
            # Центроид каждого raw-кластера (по ВСЕМ эмбеддингам sequence) перематчивается
            # на сохранённые reference-центроиды; совпавшие получают старый stable_id,
            # новые speakers получают max(id)+1. References обновляются скользящим средним.
            stable_map = {}
            refs = self.sequence_id_speaker_refs.get(corrid, {})
            raw_centroids = {}
            if out_embds and i > 0:
                emb_list = out_embds[0]
                try:
                    labels_all = np.asarray(result, dtype=np.int64)
                    if len(labels_all) == len(emb_list):
                        for lab in np.unique(labels_all):
                            idx = np.where(labels_all == lab)[0]
                            vec = np.mean(
                                np.stack([np.asarray(emb_list[j], dtype=np.float64) for j in idx]),
                                axis=0)
                            raw_centroids[int(lab)] = pf_normalize(vec)
                        stable_map, self.sequence_id_speaker_refs[corrid], pf_matches, pf_new_ids = \
                            pf_stable_speaker_mapping(raw_centroids, refs)
                        logging.info(
                            f"PF:FINALIZE corrid={corrid} embeddings={len(emb_list)} "
                            f"refs_before={sorted(refs.keys())} "
                            f"refs_after={sorted(self.sequence_id_speaker_refs[corrid].keys())} "
                            f"matches={[(r, s, round(c, 4)) for r, s, c in pf_matches]} "
                            f"new_speakers={pf_new_ids}")
                except Exception as e:
                    logging.warning(
                        f"PF:FINALIZE corrid={corrid} stable mapping failed: {e}. Falling back to raw labels")
                    stable_map = {}

            # Return the batched resoponse
            st = 0
            for b in batch_count:
                diarization_result = output_pb2.DiarisationResponse()
                results_chechked_splitted = []
                sents = np.array(results[st:st + b])
                logging.info(f"Process {corrid} is empty")
                logging.info(f"diarization_result {diarization_result}")
                if False:
                    logging.info(f"Process {corrid} is empty")
                    new_speaker_segment = output_pb2.SpeakerSegment()
                    new_begin_vad_mark = utils.convert_to_vad_mark(0, MARK_BEGIN)
                    new_end_vad_mark = utils.convert_to_vad_mark(0, MARK_END)
                    new_speaker_segment.va_marks.extend([new_begin_vad_mark, new_end_vad_mark])
                    new_speaker_segment.speaker_id = 0
                    diarization_result.speaker_segment.append(new_speaker_segment)
                else:
                    if  audiogram_vad_marks != []:
                        if sents.shape[0] > 0:
                            for segment in sents[0]:
                                if self.use_vad:
                                    begin_vad_mark = utils.convert_to_vad_mark(segment[0], MARK_BEGIN)
                                    end_vad_mark = utils.convert_to_vad_mark(segment[1], MARK_END)
                                    raw_interval = begin_vad_mark.offset_ms, end_vad_mark.offset_ms
                                    # Do not allow final marks to go outside the original audiogram VAD intervals.
                                    adjusted_interval = utils.check_and_adjust_interval(audiogram_vad_marks, raw_interval)
                                    splitted_adjusted_intervals = utils.split_intervals(adjusted_interval, max_interval=ASR_MAX)
                                    for start, end in splitted_adjusted_intervals:
                                        # PF-патч: стабильный speaker ID вместо пересчёта raw-метки
                                        results_chechked_splitted.append(
                                            [start, end, stable_map.get(int(segment[2]), int(segment[2]) + 1)])
                                else:
                                    start_ms = int(segment[0] * 1000)
                                    end_ms = int(segment[1] * 1000)
                                    for start, end in utils.split_intervals((start_ms, end_ms), max_interval=ASR_MAX):
                                        # PF-патч: стабильный speaker ID вместо пересчёта raw-метки
                                        results_chechked_splitted.append(
                                            [start, end, stable_map.get(int(segment[2]), int(segment[2]) + 1)])
                                
                    final_merged_results = utils.merge_speaker_segments(results_chechked_splitted)
                    final_merged_results = utils.split_intervals_with_speaker(final_merged_results, max_interval=ASR_MAX)

                    for start, end, spk_id in final_merged_results:
                        new_speaker_segment = output_pb2.SpeakerSegment()
                        new_begin_vad_mark = utils.convert_to_vad_mark(start/1000, MARK_BEGIN)
                        new_end_vad_mark = utils.convert_to_vad_mark(end/1000, MARK_END)
                        new_speaker_segment.va_marks.extend([new_begin_vad_mark, new_end_vad_mark])
                        new_speaker_segment.speaker_id = spk_id
                        diarization_result.speaker_segment.append(new_speaker_segment)
                    logging.info(f"diarization_result {diarization_result}")
                    
                    # else:
                    #     speaker_segment.va_marks.extend([begin_vad_mark, end_vad_mark])
                    #     speaker_segment.speaker_id = int(segment[2]) + 1
                    #     diarization_result.speaker_segment.append(speaker_segment)

                output_tensors = pb_utils.Tensor("DiarisationResponse", np.array(diarization_result.SerializeToString(),  dtype=np.object_))
                inference_response = pb_utils.InferenceResponse(output_tensors=[output_tensors])
            
                responses.append(inference_response)
                st += b
            end_cls_time = time.time()

            inf_time = round(end_cls_time-start_cls_time, 3) or 0.01
            logging.info(f"3:Clustering        | Process {corrid} sequence {chunk_id} chunk{round(cur_len_sec, 2)} sec. in {inf_time} sec. RTF: {round(cur_len_sec/inf_time)}")
            inf_time = round(end_cls_time - vad_start_time, 3) or 0.01
            logging.info(f"Diarization         | Process {corrid} sequence {chunk_id} chunk {round(cur_len_sec, 2)} sec. in {inf_time} sec. RTF: {round(cur_len_sec/inf_time)}")

            # PF-патч: FULL (sequence_end) — освобождение состояния и references;
            # PARTIAL — полный снапшот возвращён, состояние (embeddings, chunk-состояние,
            # references, стабильные ID) СОХРАНЯЕТСЯ до FULL.
            if end_signal:
                del self.sequence_id_embeddings[corrid]
                del self.sequence_id_vad[corrid]
                #del self.sequence_id_vad_audiogram[corrid]
                del self.sequence_id_vad_mark_audiogram[corrid]
                del self.sequence_id_last_update[corrid]
                if self.true_online:
                    del self.sequence_id_pyannet_state[corrid]
                    del self.sequence_id_wav[corrid]
                if corrid in self.sequence_id_speaker_refs:
                    del self.sequence_id_speaker_refs[corrid]
                logging.info(
                    f"PF:FULL corrid={corrid} state RELEASED "
                    f"n_segments_returned={len(final_merged_results)}")
            else:
                logging.info(
                    f"PF:PARTIAL corrid={corrid} state PRESERVED "
                    f"chunks={len(self.sequence_id_vad[corrid])} "
                    f"speaker_refs={sorted(self.sequence_id_speaker_refs.get(corrid, {}).keys())} "
                    f"n_segments_returned={len(final_merged_results)}")

            response_sender.send(inference_response,flags=pb_utils.TRITONSERVER_RESPONSE_COMPLETE_FINAL)
        else:
            response_sender.send(flags=pb_utils.TRITONSERVER_RESPONSE_COMPLETE_FINAL)
