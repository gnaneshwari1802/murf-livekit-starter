'use client';

import { useEffect, useRef, useState } from 'react';
import { useTheme } from 'next-themes';
import { AnimatePresence, motion } from 'motion/react';
import { useAgent, useSessionContext } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { AgentSessionView_01 } from '@/components/agents-ui/blocks/agent-session-view-01';
import { WelcomeView } from '@/components/app/welcome-view';

const MotionWelcomeView = motion.create(WelcomeView);
const MotionSessionView = motion.create(AgentSessionView_01);
const VIEW_MOTION_PROPS = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: 0.35, ease: 'linear' },
};

interface ViewControllerProps {
  appConfig: AppConfig;
}

export function ViewController({ appConfig }: ViewControllerProps) {
  const { isConnected, start, end } = useSessionContext();
  const { state: agentState } = useAgent();
  const { resolvedTheme } = useTheme();
  const [callState, setCallState] = useState<'ready' | 'connecting' | 'live' | 'ended'>('ready');
  const [microphoneError, setMicrophoneError] = useState<string | null>(null);
  const [outboundPhoneNumber, setOutboundPhoneNumber] = useState('');
  const [outboundState, setOutboundState] = useState<
    'ready' | 'calling' | 'connected' | 'ended' | 'error'
  >('ready');
  const [outboundError, setOutboundError] = useState<string | null>(null);
  const outboundRoomName = useRef<string | null>(null);
  const outboundConnected = useRef(false);
  const outboundRequestedAt = useRef<number | null>(null);

  useEffect(() => {
    if (callState === 'connecting' && isConnected) setCallState('live');
    if (callState === 'live' && !isConnected) setCallState('ended');
    if (callState === 'connecting' && agentState === 'failed') setCallState('ended');
  }, [agentState, callState, isConnected]);

  const explainMicrophoneError = (error: unknown) => {
    const name = error instanceof DOMException ? error.name : '';

    return name === 'NotAllowedError' || name === 'SecurityError'
      ? 'You blocked microphone access. Select the lock icon next to this page address, allow Microphone, then try again.'
      : 'We could not access your microphone. Check that it is connected and available to your browser, then try again.';
  };

  const startCall = async () => {
    setMicrophoneError(null);

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      setCallState('connecting');
      await start();
    } catch (error) {
      setMicrophoneError(explainMicrophoneError(error));
      setCallState('ready');
    }
  };

  const endCall = () => {
    setCallState('ended');
    end();
  };

  useEffect(() => {
    if (outboundState !== 'calling' && outboundState !== 'connected') return;

    const pollStatus = async () => {
      if (!outboundRoomName.current) return;
      try {
        const response = await fetch(
          `/api/outbound-call?room_name=${encodeURIComponent(outboundRoomName.current)}`,
          { cache: 'no-store' }
        );
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || 'Unable to read call status.');
        if (body.status === 'connected') {
          outboundConnected.current = true;
          setOutboundState('connected');
        } else if (body.status === 'ended') {
          setOutboundState('ended');
        } else if (outboundConnected.current) {
          setOutboundState('ended');
        } else if (
          outboundRequestedAt.current &&
          Date.now() - outboundRequestedAt.current > 60_000
        ) {
          setOutboundState('error');
          setOutboundError(
            'No connection was confirmed. The call may not have been answered or telephony setup may need attention.'
          );
        }
      } catch (error) {
        setOutboundState('error');
        setOutboundError(error instanceof Error ? error.message : 'Unable to read call status.');
      }
    };

    void pollStatus();
    const intervalId = window.setInterval(() => void pollStatus(), 2_000);
    return () => window.clearInterval(intervalId);
  }, [outboundState]);

  const startOutboundCall = async () => {
    setOutboundError(null);
    outboundConnected.current = false;
    try {
      const response = await fetch('/api/outbound-call', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone_number: outboundPhoneNumber }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'Unable to request outbound call.');
      outboundRoomName.current = body.room_name;
      outboundRequestedAt.current = Date.now();
      setOutboundState('calling');
    } catch (error) {
      setOutboundState('error');
      setOutboundError(error instanceof Error ? error.message : 'Unable to request outbound call.');
    }
  };
  const showSession = callState === 'live' && isConnected;

  return (
    <AnimatePresence mode="wait">
      {!showSession && (
        <MotionWelcomeView
          key={callState === 'ended' ? 'ended' : 'welcome'}
          {...VIEW_MOTION_PROPS}
          startButtonText={callState === 'ended' ? 'Start a new call' : appConfig.startButtonText}
          onStartCall={startCall}
          isConnecting={callState === 'connecting'}
          microphoneError={microphoneError}
          outboundPhoneNumber={outboundPhoneNumber}
          onOutboundPhoneNumberChange={setOutboundPhoneNumber}
          onStartOutboundCall={startOutboundCall}
          outboundState={outboundState}
          outboundError={outboundError}
        />
      )}
      {showSession && (
        <MotionSessionView
          key="session-view"
          {...VIEW_MOTION_PROPS}
          preConnectMessage="I'm here and listening. Tell me what you need help with."
          supportsChatInput={appConfig.supportsChatInput}
          supportsVideoInput={appConfig.supportsVideoInput}
          supportsScreenShare={appConfig.supportsScreenShare}
          isPreConnectBufferEnabled={appConfig.isPreConnectBufferEnabled}
          audioVisualizerType={appConfig.audioVisualizerType}
          audioVisualizerColor={
            resolvedTheme === 'dark'
              ? appConfig.audioVisualizerColorDark
              : appConfig.audioVisualizerColor
          }
          audioVisualizerColorShift={appConfig.audioVisualizerColorShift}
          audioVisualizerBarCount={appConfig.audioVisualizerBarCount}
          audioVisualizerGridRowCount={appConfig.audioVisualizerGridRowCount}
          audioVisualizerGridColumnCount={appConfig.audioVisualizerGridColumnCount}
          audioVisualizerRadialBarCount={appConfig.audioVisualizerRadialBarCount}
          audioVisualizerRadialRadius={appConfig.audioVisualizerRadialRadius}
          audioVisualizerWaveLineWidth={appConfig.audioVisualizerWaveLineWidth}
          onEndCall={endCall}
          onMicrophoneError={(error) => setMicrophoneError(explainMicrophoneError(error))}
          className="fixed inset-0"
        />
      )}
    </AnimatePresence>
  );
}
