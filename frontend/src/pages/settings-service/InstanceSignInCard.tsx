import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { InfoRow, SCard } from '@/components/settings/kit'
import { authStatusQueryOptions } from '@/lib/deploymentMode'
import { INSTANCE_SIGN_IN_DOCS_URL } from '@/lib/docsSite'

/**
 * Settings › Platform › Security & access: the instance's own single sign-on,
 * read-only. One OpenID Connect provider and Google can sign everyone on the
 * instance in; both are set with environment variables, so the card only says
 * which the sign-in page offers (the unauthenticated status probe it reads)
 * and links to the setup. Without it, an operator who looked for "SSO" found
 * only the organization teaser tagged Enterprise.
 */
export function InstanceSignInCard() {
  const query = useQuery(authStatusQueryOptions())
  const status = query.data
  // Nothing before the first answer, so the card does not flash "Off".
  if (!status && !query.isError) return null
  return (
    <SCard
      title="Instance sign-in"
      description="Signing everyone on this instance in through one OpenID Connect provider (Okta, Entra ID, Keycloak and others) or Google. Set with the OIDC_* and GOOGLE_* environment variables and read at startup, not here."
      footer={
        <a
          href={INSTANCE_SIGN_IN_DOCS_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-body-sm font-medium text-accent no-underline hover:underline"
        >
          How to set it up
          <ExternalLink className="size-3.5" aria-hidden="true" />
        </a>
      }
    >
      {status ? (
        <>
          <InfoRow
            label="OpenID Connect"
            value={
              status.oidc_sign_in === true
                ? `On: the sign-in page offers “${status.oidc_button_label || 'Sign in with OpenID Connect'}”`
                : 'Not configured'
            }
            mono={false}
          />
          <InfoRow
            label="Google"
            value={status.google_sign_in === true ? 'On: the sign-in page offers “Continue with Google”' : 'Not configured'}
            mono={false}
            last
          />
        </>
      ) : (
        <InfoRow label="OpenID Connect" value="Unknown (the status could not be read)" mono={false} last />
      )}
    </SCard>
  )
}
