select
    account_id,
    account_name,
    lower(trim(company_domain)) as company_domain,
    coalesce(is_customer, false) as is_customer,
    nullif(trim(account_owner_id), '') as account_owner_id,
    updated_at as account_updated_at
from {{ ref('crm_accounts') }}
