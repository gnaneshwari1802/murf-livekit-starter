'use client';

import { useEffect, useState } from 'react';
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
