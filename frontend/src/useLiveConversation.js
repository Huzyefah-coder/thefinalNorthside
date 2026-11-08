import {useEffect, useRef, useState} from 'react';
import {normalizeLiveVoiceInput} from './liveVoiceInput';

// Optional hands-free mode. The existing chat endpoint and agent remain unchanged.
export default function useLiveConversation(onTranscript, onError) {
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState('off');
  const refs = useRef({onTranscript, onError});
  refs.current.onTranscript = onTranscript;
  refs.current.onError = onError;
  const state = useRef({active: false, token: 0, recognition: null, utterance: null, timer: null, phase: 'off', lastReply: ''});

  function clearAudio() {
    const s = state.current;
    clearTimeout(s.timer);
    s.timer = null;
    if (s.recognition) {
      const r = s.recognition;
      s.recognition = null;
      r.onresult = null;
      r.onerror = null;
      r.onend = null;
      try { r.abort(); } catch (_) {}
    }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    s.utterance = null;
  }

  function stop() {
    const s = state.current;
    s.active = false;
    s.token++;
    s.phase = 'off';
    s.lastReply = '';
    clearAudio();
    setActive(false);
    setStatus('off');
  }

  function hear(token) {
    const s = state.current;
    if (!s.active || token !== s.token) return;
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      refs.current.onError('Live conversation needs browser speech recognition. Try Chrome or Edge.');
      stop();
      return;
    }
    const r = new SpeechRecognition();
    r.lang = 'en-US';
    r.continuous = false;
    r.interimResults = false;
    s.recognition = r;
    s.phase = 'listening';
    setStatus('listening');
    let received = false;
    r.onresult = async e => {
      if (received || !s.active || token !== s.token) return;
      received = true;
      const transcript = Array.from(e.results).map(x => x[0]?.transcript || '').join(' ').trim();
      if (!transcript) return;
      s.phase = 'thinking';
      try { r.stop(); } catch (_) {}
      setStatus('thinking');
      try {
        const reply = await refs.current.onTranscript(normalizeLiveVoiceInput(transcript, s.lastReply));
        if (!s.active || token !== s.token) return;
        if (reply) speak(reply, token);
        else stop(); // Existing chat handler displays the error.
      } catch (e) {
        if (s.active && token === s.token) {
          refs.current.onError(e.message || 'Live conversation failed.');
          stop();
        }
      }
    };
    r.onerror = e => {
      if (!s.active || token !== s.token) return;
      if (e.error === 'no-speech' || e.error === 'aborted') return;
      refs.current.onError('Live microphone: ' + e.error);
      stop();
    };
    r.onend = () => {
      if (s.recognition === r) s.recognition = null;
      if (s.active && token === s.token && s.phase === 'listening' && !received) {
        s.timer = setTimeout(() => hear(token), 350);
      }
    };
    try { r.start(); } catch (e) {
      refs.current.onError('Could not start live microphone: ' + e.message);
      stop();
    }
  }

  function speak(text, token) {
    const s = state.current;
    if (!s.active || token !== s.token) return;
    if (!('speechSynthesis' in window)) {
      refs.current.onError('Spoken replies are unavailable in this browser.');
      stop();
      return;
    }
    s.phase = 'speaking';
    s.lastReply = text;
    setStatus('speaking');
    const u = new SpeechSynthesisUtterance(text);
    u.rate = .95;
    s.utterance = u;
    let finished = false;
    const next = () => {
      if (finished) return;
      finished = true;
      if (s.active && token === s.token) hear(token);
    };
    u.onend = next;
    u.onerror = e => {
      if (e.error === 'canceled' || e.error === 'interrupted') return;
      next();
    };
    window.speechSynthesis.speak(u);
  }

  function start() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR || !('speechSynthesis' in window)) {
      refs.current.onError('Live voice conversation requires speech recognition and speech synthesis (try Chrome or Edge).');
      return;
    }
    stop();
    const s = state.current;
    s.active = true;
    const token = s.token;
    setActive(true);
    speak('Hello! Welcome to Northside Clinic. How can I help you today?', token);
  }

  useEffect(() => () => {
    const s = state.current;
    s.active = false;
    s.token++;
    clearTimeout(s.timer);
    if (s.recognition) {
      s.recognition.onend = null;
      try { s.recognition.abort(); } catch (_) {}
    }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  }, []);

  return {liveActive: active, liveStatus: status, startLive: start, stopLive: stop};
}
