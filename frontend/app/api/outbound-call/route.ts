import { NextResponse } from 'next/server';
import { AgentDispatchClient, RoomServiceClient } from 'livekit-server-sdk';

const E164_PHONE_NUMBER = /^\+[1-9]\d{7,14}$/;
const LIVEKIT_URL = process.env.LIVEKIT_URL;
const LIVEKIT_API_KEY = process.env.LIVEKIT_API_KEY;
const LIVEKIT_API_SECRET = process.env.LIVEKIT_API_SECRET;
const AGENT_NAME = process.env.AGENT_NAME || 'my-agent';

export const dynamic = 'force-dynamic';

function maskPhoneNumber(phoneNumber: string) {
  return `${phoneNumber.slice(0, 3)}${'*'.repeat(Math.max(0, phoneNumber.length - 5))}${phoneNumber.slice(-2)}`;
}

function isMissingRoomError(error: unknown) {
  if (!error || typeof error !== 'object') return false;

  const candidate = error as { code?: unknown; message?: unknown; status?: unknown };
  return (
    candidate.status === 404 ||
    candidate.code === 5 ||
    (typeof candidate.message === 'string' &&
      /requested room does not exist|room.*not found/i.test(candidate.message))
  );
}

function clients() {
  if (!LIVEKIT_URL || !LIVEKIT_API_KEY || !LIVEKIT_API_SECRET) {
    throw new Error('LiveKit credentials are not configured for outbound calling.');
  }

  return {
    dispatch: new AgentDispatchClient(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET),
    rooms: new RoomServiceClient(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET),
  };
}

export async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => ({}));
    const phoneNumber = body?.phone_number || process.env.OUTBOUND_DESTINATION_PHONE_NUMBER;

    if (typeof phoneNumber !== 'string' || !E164_PHONE_NUMBER.test(phoneNumber)) {
      return NextResponse.json(
        {
          error:
            'Enter a valid destination phone number in E.164 format, for example +919876543210.',
        },
        { status: 400 }
      );
    }

    const roomName = `health-follow-up-${crypto.randomUUID()}`;
    const { dispatch, rooms } = clients();
    console.info('Outbound call requested', {
      roomName,
      destination: maskPhoneNumber(phoneNumber),
    });
    await rooms.createRoom({ name: roomName, emptyTimeout: 60, departureTimeout: 30 });
    console.info('Outbound call room created', { roomName });
    await dispatch.createDispatch(roomName, AGENT_NAME, {
      metadata: JSON.stringify({ phone_number: phoneNumber, call_type: 'health_follow_up' }),
    });
    console.info('Outbound agent dispatched', { roomName });

    return NextResponse.json({ room_name: roomName, status: 'calling' }, { status: 202 });
  } catch (error) {
    console.error('Unable to request outbound call', error);
    return NextResponse.json(
      {
        error:
          'The outbound call could not be requested. Check the server logs and telephony configuration.',
      },
      { status: 502 }
    );
  }
}

export async function GET(request: Request) {
  try {
    const roomName = new URL(request.url).searchParams.get('room_name');
    if (!roomName || !/^health-follow-up-[a-f0-9-]{36}$/.test(roomName)) {
      return NextResponse.json({ error: 'Invalid outbound call reference.' }, { status: 400 });
    }

    const { rooms } = clients();
    const participants = await rooms.listParticipants(roomName);
    const sipParticipant = participants.find(
      (participant) => participant.attributes['sip.callStatus']
    );
    const callStatus = sipParticipant?.attributes['sip.callStatus'];

    if (callStatus === 'active') {
      console.info('Outbound call connected', { roomName });
      return NextResponse.json({ status: 'connected' });
    }
    if (callStatus) {
      return NextResponse.json({ status: 'calling' });
    }
    return NextResponse.json({ status: 'calling' });
  } catch (error) {
    if (isMissingRoomError(error)) {
      // LiveKit removes empty rooms after a completed or unanswered call. Polling
      // this endpoint after that is expected and should not surface as a 502.
      return NextResponse.json({ status: 'ended' });
    }
    console.error('Unable to read outbound call status', error);
    return NextResponse.json(
      { error: 'Unable to read the outbound call status.' },
      { status: 502 }
    );
  }
}
