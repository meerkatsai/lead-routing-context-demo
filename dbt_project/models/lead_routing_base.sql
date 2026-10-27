-- The routing contract: one row per lead with everything the Python policy needs.
-- Keep parsing and joins here, out of prompts and out of the policy code.
with leads as (
    select * from {{ ref('stg_leads') }}
),

free_email as (
    select lower(email_domain) as email_domain from {{ ref('free_email_domains') }}
),

with_domain as (
    -- Fall back to the email domain, unless it is a personal mailbox.
    select
        l.*,
        coalesce(
            l.company_domain_provided,
            case when f.email_domain is null then nullif(l.email_domain, '') end
        ) as company_domain_normalized,
        case
            when l.company_domain_provided is not null then 'provided'
            when f.email_domain is null and nullif(l.email_domain, '') is not null then 'email'
        end as company_domain_source
    from leads l
    left join free_email f on l.email_domain = f.email_domain
),

with_country as (
    select
        d.*,
        a.country_code
    from with_domain d
    left join {{ ref('country_aliases') }} a on d.country_raw = a.raw_value
),

deduped as (
    -- Same normalized email = same person. The earliest record is the original.
    select
        *,
        case
            when email_normalized is not null
                 and row_number() over (partition by email_normalized order by created_at, lead_id) > 1
            then first_value(lead_id) over (partition by email_normalized order by created_at, lead_id)
        end as duplicate_of_lead_id
    from with_country
)

select
    d.lead_id,
    d.created_at as lead_created_at,
    d.email_normalized,
    d.company_domain_normalized,
    d.company_domain_source,
    d.country_raw,
    d.country_code,
    d.employee_count_raw,
    d.employee_count,
    d.duplicate_of_lead_id,
    d.notes,

    acc.account_id,
    acc.account_name,
    coalesce(acc.is_customer, false) as is_existing_customer,
    acc.account_owner_id,
    acc.account_updated_at,

    coalesce(opp.open_opportunity_count, 0) as open_opportunity_count,
    opp.open_opportunity_ids,
    opp.open_opportunity_owner_ids,
    opp.opportunity_updated_at,

    t.territory_owner_id,

    concat_ws(',',
        case when d.email_normalized is null then 'email' end,
        case when d.company_domain_normalized is null then 'company_domain' end,
        case when d.country_code is null then 'country' end
    ) as missing_fields,
    (d.email_normalized is null
        or d.company_domain_normalized is null
        or d.country_code is null) as missing_required_data,

    current_timestamp as built_at
from deduped d
left join {{ ref('stg_accounts') }} acc on d.company_domain_normalized = acc.company_domain
left join {{ ref('stg_open_opportunities') }} opp on acc.account_id = opp.account_id
left join {{ ref('territory_owners') }} t on d.country_code = t.country_code
