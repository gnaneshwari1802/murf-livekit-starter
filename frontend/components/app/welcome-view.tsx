import { Headphones, LoaderCircle, MessageCircleQuestion, PhoneCall } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface WelcomeViewProps {
  startButtonText: string;
  onStartCall: () => void;
  isConnecting?: boolean;
  microphoneError?: string | null;
  outboundPhoneNumber: string;
  onOutboundPhoneNumberChange: (value: string) => void;
  onStartOutboundCall: () => void;
  outboundState?: 'ready' | 'calling' | 'connected' | 'ended' | 'error';
  outboundError?: string | null;
}

export const WelcomeView = ({
  startButtonText,
  onStartCall,
  isConnecting = false,
  microphoneError,
  outboundPhoneNumber,
  onOutboundPhoneNumberChange,
  onStartOutboundCall,
  outboundState = 'ready',
  outboundError,
  ref,
}: React.ComponentProps<'div'> & WelcomeViewProps) => {
  return (
    <div ref={ref} className="w-full px-5">
      <section className="mx-auto flex max-w-xl flex-col items-center justify-center text-center">
        <div className="mb-7 flex size-16 items-center justify-center rounded-3xl bg-teal-700 text-white shadow-lg shadow-teal-900/20">
          <Headphones className="size-8" aria-hidden="true" />
        </div>
        <p className="mb-3 rounded-full bg-teal-50 px-3 py-1 font-mono text-[11px] font-bold tracking-wider text-teal-800 uppercase dark:bg-teal-400/10 dark:text-teal-300">
          Health Access, on your terms
        </p>
        <h1 className="text-3xl font-semibold tracking-tight md:text-5xl">
          {isConnecting ? 'Connecting you to Aarogya Sahayak' : 'Talk about your health concern'}
        </h1>
        <p className="text-muted-foreground mt-4 max-w-md leading-6">
          {isConnecting
            ? 'Please wait while your private Health Access call is set up.'
            : 'Get brief general health information and guidance to appropriate care - just speak naturally.'}
        </p>

        {microphoneError && (
          <div
            role="alert"
            className="mt-6 w-full rounded-2xl border border-amber-300 bg-amber-50 p-4 text-left text-sm text-amber-950 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-100"
          >
            <div className="mb-1 flex items-center gap-2 font-semibold">
              <MessageCircleQuestion className="size-4" />
              Microphone access is needed
            </div>
            <p>{microphoneError}</p>
          </div>
        )}

        <Button
          size="lg"
          onClick={onStartCall}
          disabled={isConnecting}
          className="mt-8 h-12 w-full max-w-xs rounded-full bg-teal-700 font-mono text-xs font-bold tracking-wider uppercase hover:bg-teal-800 dark:bg-teal-500 dark:text-slate-950 dark:hover:bg-teal-400"
        >
          {isConnecting && <LoaderCircle className="animate-spin" />}
          {isConnecting ? 'Connecting...' : startButtonText}
        </Button>
        {!isConnecting && (
          <p className="text-muted-foreground mt-5 text-xs">
            Your microphone is only used during this call.
          </p>
        )}

        <div className="mt-10 w-full max-w-xs border-t pt-6 text-left">
          <p className="text-center font-mono text-xs font-bold tracking-wider uppercase">
            Health follow-up call
          </p>
          <p className="text-muted-foreground mt-2 text-center text-xs">
            Status:{' '}
            {outboundState === 'ready'
              ? 'Ready'
              : outboundState === 'calling'
                ? 'Calling'
                : outboundState === 'connected'
                  ? 'Connected'
                  : outboundState === 'ended'
                    ? 'Call ended'
                    : 'Error'}
          </p>
          <label className="sr-only" htmlFor="outbound-phone-number">
            Destination phone number
          </label>
          <input
            id="outbound-phone-number"
            type="tel"
            suppressHydrationWarning
            value={outboundPhoneNumber}
            onChange={(event) => onOutboundPhoneNumberChange(event.target.value)}
            placeholder="+919876543210"
            disabled={outboundState === 'calling' || outboundState === 'connected'}
            className="bg-background mt-4 h-11 w-full rounded-md border px-3 text-sm"
          />
          {outboundError && (
            <p role="alert" className="text-destructive mt-2 text-xs">
              {outboundError}
            </p>
          )}
          <Button
            size="sm"
            onClick={onStartOutboundCall}
            disabled={outboundState === 'calling' || outboundState === 'connected'}
            className="mt-3 w-full"
          >
            {outboundState === 'calling' && <LoaderCircle className="animate-spin" />}
            <PhoneCall />
            {outboundState === 'calling' ? 'Calling...' : 'Start follow-up call'}
          </Button>
        </div>
      </section>
    </div>
  );
};
