from typing import Union
from betterconf import Config
from collections import defaultdict

import numpy as np
import logging
import time
import math

from vad.marblenet import MarbleNetVad
from vad.converter import Converter
from vad.timestamps import OnlineTimestamp
import logging

MARK_NONE = 0
MARK_BEGIN = 1
MARK_END = 2

logging.basicConfig(
    format="%(levelname)s: %(message)s",
    level=logging.INFO,
)

class OnlineHandler:
    """
    Designed to process one audio file piece by chunks.
    Stores the time of the current chunk relative to the beginning of the audio.

    NOTE:
        After processing one audio file, you need to reset the internal state using the method .reset_states()

    """

    def __init__(self, config: Config, model: MarbleNetVad):

        self.model = model
        self.set_config(config)

    def reset_states(self):
        """Reset the time of the current chunk and the internal state of the model."""
        self.states = {}

    def set_config(self, config):
        self.output_format = config.output_format
        self.inner_window_size_sec = config.inner_window_size_sec
        self.sample_rate = config.sample_rate
        self.inner_window_size_samples = int(Converter.sec_to_samples(self.inner_window_size_sec, self.sample_rate))
        self.max_speech_sec = config.max_speech_sec
        self.max_speech_samples =  int(Converter.sec_to_samples(self.max_speech_sec, self.sample_rate))
        self.reset_states()

    def get_speech(self, batch) -> Union[OnlineTimestamp, None]:
        """
        For the current chunk, return whether it is the "begin" and "end" of a speech fragment or None.

        Args:
            chunk (torch.Tensor): input chunk;
            sample_rate (int): input chunk sample rate;

        Returns:
            Union[OnlineTimestamp, None]: if a speech boundary was found, returns OnlineTimestamp describing the boundary type and time, otherwise return None.

        """
        start_time = time.time()
        logging.debug(f'Start OnlineHandler.get_speech: {start_time}')
        logging.debug(f'\ninput batch: {batch}')

        num_samples = batch.batch.shape[0]

        if num_samples == 0:
            return {corrid: [] for corrid in batch.corrid_list}

        num_windows = (num_samples + self.inner_window_size_samples - 1) // self.inner_window_size_samples
        padded_size = num_windows * self.inner_window_size_samples

        if padded_size > num_samples:
            batch.batch = np.pad(batch.batch, (0, padded_size - num_samples))

        batch.batch = batch.batch.reshape(num_windows, self.inner_window_size_samples)
        logging.debug(f'Reshape chunks: {batch.batch.shape}, {batch.batch.shape[0]} * {batch.batch.shape[1]} = {batch.batch.shape[0] * batch.batch.shape[1]} \n')

        # получаем вероятности для каждого чанка
        speech_probs = self.model.get_speech_prob(batch.batch)

        new_corrid_list = []
        corrid_to_num_chunks = {}
        for corrid_, input_audio in zip(batch.corrid_list, batch._audios):
            audio_shape = input_audio.shape[0]
            num_chunks = (audio_shape + self.inner_window_size_samples - 1) // self.inner_window_size_samples
            new_corrid_list.extend([corrid_] * int(num_chunks))
            corrid_to_num_chunks[corrid_] = num_chunks
        batch.corrid_list = new_corrid_list
        logging.debug(f'\ncorrid list: {batch.corrid_list}, num_chunks: {len(batch.corrid_list)}, num_speech_probs: {len(speech_probs)}')

        # печатает вероятность речи для каждого чанка
        # for i, (corrid, speech_prob) in enumerate(zip(batch.corrid_list, speech_probs)):
        #     logging.debug(f'i: {i}, corrid: {corrid}, speech prob: {round(speech_prob, 4)}')


        ts = defaultdict(list)
        corr_id_to_last_sample = {}
        # проходим в цикле по каждому маленькому чанку
        logging.debug(f'\ninitial states:')
        # for j in self.states:
        #     logging.debug(f' corrid: {j}, states: {self.states[j]}')
        for i, corrid in enumerate(batch.corrid_list):
            logging.debug(f'\ncorr_id: {corrid}, i: {i}')

            # если это первый чанк и для этого corrid еще нет states
            # инициализируем исходное состояние
            if batch.corrid_info[corrid]['is_start'] and corrid not in self.states:
                self.states[corrid] = {
                    "triggered": False,
                    "temp_end": 0,
                    "current_sample": 0,
                    "current_start": 0,
                    "split_by_pauses_end": False,
                    "split_by_pauses_begin": False,
                    "num_inner_chunk": 1,
                }
                logging.debug(f'init states for corrid: {corrid}')
                logging.debug(f'init states: {self.states[corrid]}')

            # текущее состояние  для corrid
            logging.debug(f'states: {self.states[corrid]}')

            # обновляем время теущего чанка для даного corrid
            self.states[corrid]["current_sample"] += self.inner_window_size_samples
            self.states[corrid]["current_start"] = self.states[corrid]["current_sample"] - self.inner_window_size_samples
            logging.debug(f'update current_sample: {self.states[corrid]["current_sample"]}')
            logging.debug(f'update current_start:  {self.states[corrid]["current_start"]}')

            # получаем таймстемп для текущего corrid
            logging.debug(f'corrid: {corrid}, i: {i}, speech_prob: {speech_probs[i]}')
            timestamp = self._get_timestamp(speech_probs[i], corrid, batch)
            if batch.corrid_info[corrid]['mode'] == 'SPLIT_BY_PAUSES':
                logging.debug('split by pauses mode')
                timestamp = self.use_split_by_pauses_timestamp_format(timestamp, batch, corrid)
            logging.debug(f'corrid: {corrid}, i: {i}, timestamp: {timestamp}')

            # если это последний чанк входного сигнала и последний маленький чанк для данного corrid
            # удаляем внутреннее состояние для данного corrid
            if batch.corrid_info[corrid]['is_end']:
                logging.debug(f'num_inner_chunk: {self.states[corrid]["num_inner_chunk"]}, from {corrid_to_num_chunks[corrid]}')
                if self.states[corrid]["num_inner_chunk"] == corrid_to_num_chunks[corrid]:
                    # corr_id_to_last_sample[corrid] = self.states[corrid]["current_sample"]
                    del self.states[corrid]
                else:
                    self.states[corrid]["num_inner_chunk"] += 1

            ts[corrid].append(timestamp)

        logging.debug(f'\nResult:')
        new_corrid_to_ts = {}
        for corrid in ts:
            logging.debug(f'corrid: {corrid}')
            logging.debug(f'ts: {[arr.tolist() for arr in ts[corrid]]}')
            num_ts = len(ts[corrid])
            new_ts = []
            for i, current_ts in enumerate(ts[corrid],1):
                logging.debug(f'current_ts: {current_ts} i: {i}')
                # если метка None и это не последний чанк то пропускаем
                # иначе выдаем метку
                if current_ts[0] == MARK_NONE and i != num_ts:
                    continue
                else:
                    new_ts.append(current_ts)

                # если последняя метка беги - добовляем метку NONE на конец чанка
                if current_ts[0] == MARK_BEGIN and i == num_ts:
                    inner_window_size_ms = int(Converter.samples_to_ms(self.inner_window_size_samples, self.sample_rate))
                    end_time = current_ts[1] + inner_window_size_ms
                    new_ts.append(np.array([MARK_NONE, end_time]))

            new_corrid_to_ts[corrid] = new_ts
        logging.info(f'Result timestamps: {new_corrid_to_ts}')
        end_time = time.time()
        logging.info(f"Run time OnlineHandler.get_speech: {round(end_time - start_time, 5)} sec")
        return new_corrid_to_ts

    def use_split_by_pauses_timestamp_format(self, timestamp, batch, corrid):
        logging.info('use_split_by_pauses_timestamp_format')
        logging.info(f'corr id: {corrid}: {self.states[corrid]}')
        if batch.corrid_info[corrid]['is_start'] and self.states[corrid]["split_by_pauses_begin"] == False:
            self.states[corrid]["split_by_pauses_begin"] = True
            timestamp = np.array([MARK_BEGIN, 1])
        elif self.states[corrid]["split_by_pauses_end"]:
            if timestamp[0] == MARK_NONE:
                speech_start = self.states[corrid]["current_start"]
                speech_start = self._change_output_format(speech_start)
                timestamp[1] = speech_start
            timestamp[0] = MARK_BEGIN
            self.states[corrid]["split_by_pauses_begin"] = True
            self.states[corrid]["split_by_pauses_end"] = False
        else:
            if timestamp[0] == MARK_BEGIN:
                timestamp[0] = MARK_NONE
                timestamp[1] = self._change_output_format(self.states[corrid]["current_sample"])
            elif timestamp[0] == MARK_END:
                self.states[corrid]["split_by_pauses_begin"] = False
                self.states[corrid]["split_by_pauses_end"] = True
        return timestamp


    def _get_timestamp(self, speech_prob: float, corrid: int, batch) -> Union[OnlineTimestamp, None]:
        """
        Based on the probability of speech on current chunk and the probabilities of speech on the previous chunk,
        determine whether it is a speech boundary or not.

        Args:
            speech_prob (float): probability of speech on current chunk;
            sample_rate (int): current chunk sample rate;

        Returns:
            Union[OnlineTimestamp, None]: if a speech boundary was found, returns OnlineTimestamp describing the boundary type and time, otherwise return None.

        """
        min_silence_ms = batch.corrid_info[corrid]['min_silence_ms']
        self.min_silence_samples = Converter.ms_to_samples(min_silence_ms, self.sample_rate)
        if speech_prob >= batch.corrid_info[corrid]['threshold']:
            timestamp = self._get_timestamp_for_chunk_with_speech(corrid)
            return timestamp

        else:
            timestamp = self._get_timestamp_for_chunk_without_speech(corrid)
            return timestamp

    def _get_timestamp_for_chunk_with_speech(self, corrid: int) -> Union[OnlineTimestamp, None]:
        """
        Сheck for the current chunk whether it is the begin of a speech or not.

        Args:
            sample_rate (int): current chunk sample rate;

        Returns:
            Union[OnlineTimestamp, None]: if a speech boundary was found, returns OnlineTimestamp describing the boundary type and time, otherwise return None.

        """
        logging.debug('run get_timestamp_for_chunk_with_speech')
        if self.states[corrid]["temp_end"]:
            self.states[corrid]["temp_end"] = 0


        if self.states[corrid]["triggered"]:

            if self.states[corrid]["current_sample"] - self.states[corrid]["begin_speech_sample"] >= self.max_speech_samples:
                self.states[corrid]["temp_end"] = 0
                self.states[corrid]["triggered"] = False

                speech_end = self.states[corrid]["current_sample"] # вернуть конец чанка
                speech_end = self._change_output_format(speech_end)
                return np.array([MARK_END, speech_end])

            else:
                current_time = self._change_output_format(self.states[corrid]["current_sample"])
                return np.array([MARK_NONE, current_time])

        else:
            self.states[corrid]["triggered"] = True
            speech_start = self.states[corrid]["current_start"]
            self.states[corrid]["begin_speech_sample"] = speech_start
            speech_start = self._change_output_format(speech_start)

            return np.array([MARK_BEGIN, speech_start])

    def _get_timestamp_for_chunk_without_speech(self, corrid: int) -> Union[OnlineTimestamp, None]:
        """
        Сheck for the current chunk whether it is the end of a speech or not.

        Args:
            sample_rate (int): current chunk sample rate;

        Returns:
            Union[OnlineTimestamp, None]: if a speech boundary was found, returns OnlineTimestamp describing the boundary type and time, otherwise return None.

        """
        logging.debug('run get_timestamp_for_chunk_without_speech')
        if self.states[corrid]["triggered"]:
            if not self.states[corrid]["temp_end"]:
                self.states[corrid]["temp_end"] = self.states[corrid]["current_sample"]

            if self.states[corrid]["current_sample"] - self.states[corrid]["temp_end"] < self.min_silence_samples:
                current_time = self._change_output_format(self.states[corrid]["current_sample"])
                return np.array([MARK_NONE, current_time])

            else:
                self.states[corrid]["temp_end"] = 0
                self.states[corrid]["triggered"] = False

                speech_end = self.states[corrid]["current_sample"] # вернуть конец чанка
                speech_end = self._change_output_format(speech_end)
                return np.array([MARK_END, speech_end])

        else:
            current_time = self._change_output_format(self.states[corrid]["current_sample"])
            return np.array([MARK_NONE, current_time])

    def _change_output_format(self, timestamp_samples: int) -> Union[int, float]:
        """
        Сonvert time to desired format.

        Args:
            timestamp_samples(int): current time in samples;
            sample_rate (int): current chunk sample rate;

        Returns:
            Union[int, float]: current time in self.output_format (sec, ms or samples);

        """
        if self.output_format == "ms":
            timestamp = Converter.samples_to_ms(timestamp_samples, self.sample_rate)

        elif self.output_format == "sec":
            timestamp = Converter.samples_to_sec(timestamp_samples, self.sample_rate)

        elif self.output_format == "sample":
            timestamp = timestamp_samples

        else:
            raise NotImplementedError(f'Output format "{self.output_format}" not implemented.')

        return timestamp
