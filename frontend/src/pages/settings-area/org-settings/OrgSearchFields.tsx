import { Button } from "@/components/ui/button";
import { Field, SCard, TextInput, ToggleRow } from "@/components/settings/kit";
import {
  FIELD_COPY,
  embeddingProviderText,
} from "@/pages/settings-service/fieldCopy";
import { ReadOnlyValue } from "@/pages/settings-service/ServiceSettingsPrimitives";
import {
  EMBEDDING_GROUP,
  clearGroup,
  displayValue,
  groupOwned,
  groupWarning,
  type OrgDraft,
} from "./orgSettingsModel";
import {
  InheritHint,
  OrgFieldBadge,
  OrgTextField,
  type OrgFieldProps,
} from "./OrgSettingsPrimitives";
import { formatNumber } from '@/lib/format'

/**
 * Organization › Semantic search (F20 PR10): the embedding endpoint this
 * organization's indexed plan text is sent to. Provider, model, endpoint and
 * key are one credential group; the vector width is the platform's, so the
 * save embeds one test text with the organization's model and is refused
 * (422) unless the width matches. A change that moves the organization's
 * vector space re-embeds this organization's projects only.
 *
 * What cannot be edited here (the provider, the width, the platform's own
 * endpoint) is text, not a read-only input that looks like the editable ones
 * around it.
 */
export function OrgSearchFields({
  settings,
  draft,
  setField,
  setDraft,
  saving,
}: OrgFieldProps & {
  setDraft: (next: OrgDraft) => void;
  saving: boolean;
}) {
  const section = "search" as const;
  const props = { settings, draft, setField, section };
  const organizationScope = settings.scope === "organization";
  const warning = groupWarning(settings, draft, section);
  const owned =
    organizationScope && groupOwned(settings, section, EMBEDDING_GROUP);
  const enabled =
    displayValue(settings, draft, section, "search_embeddings_enabled") ===
    true;
  const dimensions = settings.search.search_embedding_dimensions;
  const provider = String(
    displayValue(settings, draft, section, "search_embedding_provider"),
  );

  return (
    <SCard
      title="Embeddings"
      // The group rule is about inheriting; the platform's own endpoint (the
      // self-hosted default organization's page) inherits from nothing.
      description={
        organizationScope
          ? "Provider, model, base URL and key are one group: once this organization sets any of them it stops inheriting the rest, and the platform's key is never sent to its endpoint."
          : undefined
      }
      footer={
        owned ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() =>
              setDraft(clearGroup(draft, settings, section, EMBEDDING_GROUP))
            }
            disabled={saving}
          >
            Use the platform&rsquo;s embeddings
          </Button>
        ) : undefined
      }
    >
      <ToggleRow
        label={FIELD_COPY.search_embeddings_enabled.label}
        labelRight={
          <OrgFieldBadge
            settings={settings}
            section={section}
            field="search_embeddings_enabled"
          />
        }
        hint={
          <>
            Off, search matches words only (keyword search always works).{" "}
            <InheritHint {...props} field="search_embeddings_enabled" />
          </>
        }
        value={enabled}
        onChange={(value) => setField("search_embeddings_enabled", value)}
      />
      {warning && (
        <p
          role="note"
          className="m-0 px-4 pt-2.5 text-caption text-(--warning)"
        >
          {warning.starting &&
            "Saving makes the whole embedding endpoint this organization’s own: fields you leave as they are take the built-in defaults, not the platform’s values. "}
          {warning.missingSecret &&
            "No API key of this organization’s: the platform’s key is never sent to an endpoint set here, so add one."}
        </p>
      )}
      <Field
        label="Provider"
        labelRight={
          <OrgFieldBadge
            settings={settings}
            section={section}
            field="search_embedding_provider"
          />
        }
        htmlFor={false}
      >
        <ReadOnlyValue value={embeddingProviderText(provider)} mono={false} />
      </Field>
      <OrgTextField
        {...props}
        field="search_embedding_model"
        label="Model"
        grouped
        placeholder="e.g. text-embedding-3-small"
        hint={`Must return vectors of ${formatNumber(dimensions)} values: saving embeds one test text with it and is refused otherwise.`}
      />
      <OrgTextField
        {...props}
        field="search_embedding_base_url"
        label="Base URL"
        grouped
        readOnly={!organizationScope}
        placeholder={
          organizationScope
            ? "Blank: the provider's default with your own model or key, else the platform's endpoint"
            : undefined
        }
        hint={
          organizationScope
            ? "Must be a public address."
            : "The platform’s endpoint is set by SEARCH_EMBEDDING_BASE_URL on the server, not here."
        }
      />
      <Field
        label="API key"
        labelRight={
          <OrgFieldBadge
            settings={settings}
            section={section}
            field="search_embedding_api_key"
          />
        }
        hint={
          <InheritHint {...props} field="search_embedding_api_key" grouped />
        }
      >
        <TextInput
          type="password"
          value={String(
            displayValue(settings, draft, section, "search_embedding_api_key"),
          )}
          onChange={(value) => setField("search_embedding_api_key", value)}
          placeholder={
            settings.search.search_embedding_api_key_configured
              ? "Configured — leave blank to keep"
              : "Not configured"
          }
        />
      </Field>
      <Field
        label="Dimensions"
        hint="Set by SEARCH_EMBEDDING_DIMENSIONS on the server, for every organization: the width of the search index."
        htmlFor={false}
        last
      >
        <ReadOnlyValue value={String(dimensions)} />
      </Field>
    </SCard>
  );
}
