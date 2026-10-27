-- Open opportunities rolled up to one row per account.
select
    account_id,
    count(*) as open_opportunity_count,
    string_agg(opportunity_id, ',' order by opportunity_id) as open_opportunity_ids,
    string_agg(distinct owner_id, ',' order by owner_id) as open_opportunity_owner_ids,
    max(updated_at) as opportunity_updated_at
from {{ ref('crm_opportunities') }}
where stage not in ('Closed Won', 'Closed Lost')
group by account_id
