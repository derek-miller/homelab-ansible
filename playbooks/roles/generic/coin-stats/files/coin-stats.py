import re
import sys
import time
import traceback

import click
import requests


def to_line_protocol(measurement_name, tags, fields, ts=None):
    tags = ",".join(
        "{}={}".format(
            tag,
            re.sub(r"\s+", r"\ ", tag_value.strip()) if isinstance(tag_value, str) else tag_value,
        )
        for tag, tag_value in tags.items()
        if tag_value is not None
    )
    fields = ",".join(
        "{}={}".format(field, field_value)
        for field, field_value in fields.items()
        if field_value is not None
    )
    ts = int(round(float(ts or time.time()) * 1e9))
    return "{},{} {} {}".format(measurement_name, tags, fields, ts)


@click.command()
@click.argument("coingecko_ids", nargs=-1, required=True)
@click.pass_context
def cli(ctx, coingecko_ids):
    for coin_id in coingecko_ids:
        try:
            response = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                headers={
                    "Accepts": "application/json",
                    "localization": "false",
                },
            )
            response.raise_for_status()
            stats = response.json()
            click.echo(
                to_line_protocol(
                    measurement_name="coin_stats",
                    tags={
                        "id": stats["id"],
                        "symbol": stats["symbol"],
                        "name": stats["name"],
                        "hashing_algorithm": stats["hashing_algorithm"],
                    },
                    fields={
                        "block_time_minutes": stats["block_time_in_minutes"],
                        "sentiment_votes_up_percentage": stats["sentiment_votes_up_percentage"],
                        "sentiment_votes_down_percentage": stats["sentiment_votes_down_percentage"],
                        "market_cap_rank": stats["market_cap_rank"],
                        "coingecko_rank": stats["coingecko_rank"],
                        "coingecko_score": stats["coingecko_score"],
                        "developer_score": stats["developer_score"],
                        "community_score": stats["community_score"],
                        "liquidity_score": stats["liquidity_score"],
                        "public_interest_score": stats["public_interest_score"],
                        # Market Data
                        "current_price": stats["market_data"]["current_price"]["usd"],
                        "all_time_high": stats["market_data"]["ath"]["usd"],
                        "all_time_high_change_percentage": stats["market_data"][
                            "ath_change_percentage"
                        ]["usd"],
                        "all_time_low": stats["market_data"]["atl"]["usd"],
                        "all_time_low_change_percentage": stats["market_data"][
                            "atl_change_percentage"
                        ]["usd"],
                        "market_cap": stats["market_data"]["market_cap"]["usd"],
                        "total_volume": stats["market_data"]["total_volume"]["usd"],
                        "high_24h": stats["market_data"]["high_24h"]["usd"],
                        "low_24h": stats["market_data"]["low_24h"]["usd"],
                        "price_change_24h": stats["market_data"]["price_change_24h"],
                        "price_change_percentage_24h": stats["market_data"][
                            "price_change_percentage_24h"
                        ],
                        "price_change_percentage_7d": stats["market_data"][
                            "price_change_percentage_7d"
                        ],
                        "price_change_percentage_14d": stats["market_data"][
                            "price_change_percentage_14d"
                        ],
                        "price_change_percentage_30d": stats["market_data"][
                            "price_change_percentage_30d"
                        ],
                        "price_change_percentage_60d": stats["market_data"][
                            "price_change_percentage_60d"
                        ],
                        "price_change_percentage_200d": stats["market_data"][
                            "price_change_percentage_200d"
                        ],
                        "price_change_percentage_1y": stats["market_data"][
                            "price_change_percentage_1y"
                        ],
                        "market_cap_change_24h": stats["market_data"]["market_cap_change_24h"],
                        "market_cap_change_percentage_24h": stats["market_data"][
                            "market_cap_change_percentage_24h"
                        ],
                        "price_change_24h_in_usd": stats["market_data"][
                            "price_change_24h_in_currency"
                        ]["usd"],
                        "price_change_percentage_1h_in_usd": stats["market_data"][
                            "price_change_percentage_1h_in_currency"
                        ]["usd"],
                        "price_change_percentage_24h_in_usd": stats["market_data"][
                            "price_change_percentage_24h_in_currency"
                        ]["usd"],
                        "price_change_percentage_7d_in_usd": stats["market_data"][
                            "price_change_percentage_7d_in_currency"
                        ]["usd"],
                        "price_change_percentage_14d_in_usd": stats["market_data"][
                            "price_change_percentage_14d_in_currency"
                        ]["usd"],
                        "price_change_percentage_30d_in_usd": stats["market_data"][
                            "price_change_percentage_30d_in_currency"
                        ]["usd"],
                        "price_change_percentage_60d_in_usd": stats["market_data"][
                            "price_change_percentage_60d_in_currency"
                        ]["usd"],
                        "price_change_percentage_200d_in_usd": stats["market_data"][
                            "price_change_percentage_200d_in_currency"
                        ]["usd"],
                        "price_change_percentage_1y_in_usd": stats["market_data"][
                            "price_change_percentage_1y_in_currency"
                        ]["usd"],
                        "market_cap_change_24h_in_usd": stats["market_data"][
                            "market_cap_change_24h_in_currency"
                        ]["usd"],
                        "market_cap_change_percentage_24h_in_usd": stats["market_data"][
                            "market_cap_change_percentage_24h_in_currency"
                        ]["usd"],
                        "total_supply": stats["market_data"]["total_supply"],
                        "max_supply": stats["market_data"]["max_supply"],
                        "circulating_supply": stats["market_data"]["circulating_supply"],
                        # Community Data
                        "facebook_likes": stats["community_data"]["facebook_likes"],
                        "twitter_followers": stats["community_data"]["twitter_followers"],
                        "reddit_average_posts_48h": stats["community_data"][
                            "reddit_average_posts_48h"
                        ],
                        "reddit_average_comments_48h": stats["community_data"][
                            "reddit_average_comments_48h"
                        ],
                        "reddit_subscribers": stats["community_data"]["reddit_subscribers"],
                        "reddit_accounts_active_48h": stats["community_data"][
                            "reddit_accounts_active_48h"
                        ],
                        # Developer Data
                        "developer_forks": stats["developer_data"]["forks"],
                        "developer_stars": stats["developer_data"]["stars"],
                        "developer_subscribers": stats["developer_data"]["subscribers"],
                        "developer_total_issues": stats["developer_data"]["total_issues"],
                        "developer_closed_issues": stats["developer_data"]["closed_issues"],
                        "developer_open_issues": stats["developer_data"]["total_issues"]
                        - stats["developer_data"]["closed_issues"]
                        if stats["developer_data"]["total_issues"] is not None
                        and stats["developer_data"]["closed_issues"] is not None
                        else None,
                        "developer_pull_requests_merged": stats["developer_data"][
                            "pull_requests_merged"
                        ],
                        "developer_pull_request_contributors": stats["developer_data"][
                            "pull_request_contributors"
                        ],
                        "developer_code_additions_4_weeks": stats["developer_data"][
                            "code_additions_deletions_4_weeks"
                        ]["additions"],
                        "developer_code_deletions_4_weeks": stats["developer_data"][
                            "code_additions_deletions_4_weeks"
                        ]["deletions"],
                        "developer_commit_count_4_weeks": stats["developer_data"][
                            "commit_count_4_weeks"
                        ],
                    },
                ),
                file=sys.stdout,
            )
        except Exception:
            click.echo(traceback.format_exc(), err=True)
            ctx.exit(1)


if __name__ == "__main__":
    cli()
