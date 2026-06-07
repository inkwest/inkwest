import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

// OAuth redirect target for connectors. The backend has already exchanged the
// code and stored the credential in RAM; this page just reports the outcome and
// returns the user to where they were investigating.
export function ConnectorCallbackPage() {
  const navigate = useNavigate();
  const [message, setMessage] = useState('Finishing connection…');

  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.substring(1));
    const connector = params.get('connector');
    const status = params.get('status');
    const reason = params.get('reason');
    const sessionId = params.get('session_id');

    // Return to the originating session. Prefer the session_id passed back in the
    // redirect (reliable across the cross-origin OAuth round-trip); fall back to
    // sessionStorage, then root.
    const back = sessionId
      ? `/session/${sessionId}`
      : sessionStorage.getItem('connector_return_to') || '/';
    sessionStorage.removeItem('connector_return_to');

    if (status && status !== 'error') {
      setMessage(`${connector ?? 'Connector'} connected. Returning…`);
      const t = setTimeout(() => navigate(back, { replace: true }), 800);
      return () => clearTimeout(t);
    }
    setMessage(`Connection failed${reason ? `: ${decodeURIComponent(reason)}` : ''}.`);
    const t = setTimeout(() => navigate(back, { replace: true }), 2500);
    return () => clearTimeout(t);
  }, [navigate]);

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-900 text-gray-300">
      <p>{message}</p>
    </div>
  );
}
