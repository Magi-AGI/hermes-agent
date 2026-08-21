import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import type { VoiceActivityState, VoiceStatus } from '../types'

import { useMicRecorder } from './use-mic-recorder'

// A silent capture (mic held in exclusive mode by another app, or a wrong/muted
// default input device) produces a header-only webm of a few hundred bytes with
// no audio frames — the backend can then only fail it at the ffmpeg stage with an
// opaque EBML error. A real ~1s opus recording is several KB, so anything below
// this is treated as "no audio captured" and reported legibly instead of shipped.
const MIN_TRANSCRIBABLE_AUDIO_BYTES = 1024

interface VoiceRecorderOptions {
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  focusInput: () => void
  onTranscript: (text: string) => void
}

export function useVoiceRecorder({
  maxRecordingSeconds,
  onTranscribeAudio,
  focusInput,
  onTranscript
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)

  const clearTimers = () => {
    if (intervalRef.current) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    if (timeoutRef.current) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }

  useEffect(() => () => clearTimers(), [])

  const stop = async () => {
    clearTimers()
    const result = await handle.stop()

    if (!result) {
      setVoiceStatus('idle')

      return
    }

    if (!onTranscribeAudio) {
      setVoiceStatus('idle')

      return
    }

    // Empty/silent capture: report what to check rather than shipping an
    // unusable header-only file to the transcriber. Byte-size (not heardSpeech)
    // is the signal — plain push-to-talk dictation never arms the meter's
    // speech threshold, so heardSpeech stays false even for real speech.
    if (result.audio.size < MIN_TRANSCRIBABLE_AUDIO_BYTES) {
      notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.noAudioCaptured })
      setVoiceStatus('idle')
      focusInput()

      return
    }

    setVoiceStatus('transcribing')

    try {
      const transcript = (await onTranscribeAudio(result.audio)).trim()

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      setVoiceStatus('idle')
      focusInput()
    }
  }

  const start = async () => {
    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return
    }

    try {
      await handle.start({ onError: error => notifyError(error, voiceCopy.recordingFailed) })
      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      intervalRef.current = window.setInterval(() => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000), 250)
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)
    } catch (error) {
      setVoiceStatus('idle')
      notifyError(error, voiceCopy.recordingFailed)
    }
  }

  const dictate = () => {
    if (recording) {
      void stop()
    } else if (voiceStatus === 'idle') {
      void start()
    }
  }

  const voiceActivityState: VoiceActivityState = {
    elapsedSeconds,
    level,
    status: voiceStatus
  }

  return { dictate, voiceActivityState, voiceStatus }
}
