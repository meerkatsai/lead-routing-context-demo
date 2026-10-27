-- One row per raw lead, with every field the policy reads cleaned up.
with raw as (
    select * from {{ ref('crm_leads') }}
),

cleaned as (
    select
        lead_id,
        created_at,
        nullif(lower(trim(email)), '') as email_normalized,
        nullif(lower(trim(country)), '') as country_raw,
        employee_count as employee_count_raw,
        -- "1,200" -> 1200; "unknown" -> null
        try_cast(replace(trim(employee_count), ',', '') as integer) as employee_count,
        -- "https://www.orbitkiln.com/" -> "orbitkiln.com"
        nullif(
            regexp_replace(
                regexp_replace(lower(trim(company_domain)), '^(https?://)?(www\.)?', ''),
                '/.*$', ''
            ),
            ''
        ) as company_domain_provided,
        notes
    from raw
)

select
    c.*,
    split_part(c.email_normalized, '@', 2) as email_domain
from cleaned c
