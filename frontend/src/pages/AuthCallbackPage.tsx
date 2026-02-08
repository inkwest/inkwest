import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getLoginUrl } from '@/services/adminApi';

export function AuthCallbackPage() {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const hash = window.location.hash.substring(1);
    const params = new URLSearchParams(hash);
    const token = params.get('token');
    const err = params.get('error');

    if (token) {
      localStorage.setItem('session_token', token);
      navigate('/', { replace: true });
    } else if (err) {
      setError(decodeURIComponent(err));
    } else {
      setError('No token received');
    }
  }, [navigate]);

  if (!error) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <p className="text-muted-foreground">Signing in…</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-screen gap-4">
      <p className="text-destructive">Login failed: {error}</p>
      <a href={getLoginUrl()} className="text-primary underline">
        Try again
      </a>
    </div>
  );
}
