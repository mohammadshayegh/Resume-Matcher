/**
 * Sign-out. POST only: a GET would let any page (or a prefetch) log the user
 * out with an <img> tag.
 */

import { NextResponse, type NextRequest } from 'next/server';

import { getSupabaseServerClient } from '@/lib/supabase/server';

export async function POST(request: NextRequest) {
  const supabase = await getSupabaseServerClient();
  if (supabase !== null) {
    // Clears the session cookies on the response Supabase writes through.
    await supabase.auth.signOut();
  }
  return NextResponse.redirect(new URL('/login', request.nextUrl.origin), {
    // 303 so the browser follows with GET rather than replaying the POST.
    status: 303,
  });
}
