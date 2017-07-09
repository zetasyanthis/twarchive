#!/usr/bin/env python
#
# Load favorites for a Twitter user and output them to a file.
#

import csv
import logging
import json
import os
import sys
import time
import twitter

from collections import namedtuple
from sqlalchemy.orm.exc import NoResultFound
from time import sleep
from twitter.error import TwitterError

from myarchive.db.tag_db.tables.twittertables import Tweet, TwitterUser

LOGGER = logging.getLogger(__name__)


USER = "USER"
FAVORITES = "FAVORITES"

KEYS = [
    u'user',
    u'text',
    u'in_reply_to_screen_name',
    u'media',

    # Not that interesting, but saved.
    u'hashtags',
    u'id',
    u'in_reply_to_status_id',
    # Seems to be empty a lot. Put it at the end.
    u'urls',

    # Don't really care about these.
    # u'user_mentions',
    # u'source',
    # u'created_at',
    # u'id_str',
    # u'place',
    # u'in_reply_to_user_id',
    # u'lang',
    # u'possibly_sensitive',
    # u'favorited',
    # u'favorite_count',
    # u'retweeted',
    # u'retweet_count',
]


CSVTweet = namedtuple(
    'CSVTweet',
    ["id",
     "username",
     "in_reply_to_status_id",
     "in_reply_to_user_id",
     "timestamp",
     "text",
     "retweeted_status_id",
     "retweeted_status_user_id",
     "retweeted_status_timestamp",
     "expanded_urls"]
)


class TwitterAPI(twitter.Api):
    """API with an extra call."""

    def __init__(self, **kwargs):
        super(TwitterAPI, self).__init__(sleep_on_rate_limit=True, **kwargs)

    def LookupStatuses(self, status_ids, trim_user=False,
                       include_entities=True):
        """
        Returns up to 100 status messages, specified by a list passed to
        status_ids.

        Args:
          status_ids:
            A list of numeric IDs of the statuses you are trying to retrieve.
          trim_user:
            When set to True, each tweet returned in a timeline will include
            a user object including only the status authors numerical ID.
            Omit this parameter to receive the complete user object. [Optional]
          include_entities:
            If False, the entities node will be disincluded.
            This node offers a variety of metadata about the tweet in a
            discreet structure, including: user_mentions, urls, and
            hashtags. [Optional]
        Returns:
          A twitter.Status instance representing that status message
        """
        url = '%s/statuses/lookup.json' % self.base_url

        parameters = dict()

        if not status_ids or len(status_ids) > 100:
            raise TwitterError(
                "status_ids must be between 1 and 100 in length.")
        # This takes a comma-separated list of up to 100 IDs.
        parameters['id'] = ",".join(status_ids)

        if trim_user:
            parameters['trim_user'] = True
        if include_entities:
            parameters['include_entities'] = True

        resp = self._RequestUrl(url, 'GET', data=parameters)
        data = self._ParseAndCheckTwitter(resp.content.decode('utf-8'))

        return [twitter.Status.NewFromJsonDict(x) for x in data]

    @classmethod
    def import_tweets_from_api(cls, database, config, tweet_storage_path,
                               media_storage_path):
        for config_section in config.sections():
            if config_section.startswith("Twitter_"):
                api = cls(
                    consumer_key=config.get(
                        section=config_section, option="consumer_key"),
                    consumer_secret=config.get(
                        section=config_section, option="consumer_secret"),
                    access_token_key=config.get(
                        section=config_section, option="access_key"),
                    access_token_secret=config.get(
                        section=config_section, option="access_secret"),
                )
                api.archive_tweets(
                    database=database,
                    username=config.get(
                        section=config_section, option="username"),
                    tweet_storage_path=tweet_storage_path,
                    media_storage_path=media_storage_path,
                )

    def archive_tweets(self, database, username, tweet_storage_path,
                       media_storage_path):
        """
        Archives several types of new tweets along with their associated
        content.
        """
        existing_tweet_ids = database.get_existing_tweet_ids()

        new_tweets = []
        for type_ in (USER, FAVORITES):
            # Always start with None to pick up max number of new tweets.
            since_id = None
            start_time = -1
            sleep_time = 0
            max_id = None
            early_termination = False
            request_index = 0
            requests_before_sleeps = 1
            statuses = []
            while not early_termination:
                # Twitter rate-limits us. Space this out a bit to avoid a
                # super-long sleep at the end doesn't kill the connection.
                if request_index >= requests_before_sleeps:
                    # If we hit the rate limit, download media while we wait.
                    duration = time.time() - start_time
                    if duration < sleep_time:
                        for new_tweet in new_tweets:
                            new_tweet.download_media(
                                db_session=database.session,
                                media_path=media_storage_path)
                        # If we're still too fast, wait however long we need to.
                        duration = time.time() - start_time
                        if duration < sleep_time:
                            sleep_duration = sleep_time - duration
                            LOGGER.info(
                                "Sleeping for %s seconds to ease up on rate "
                                "limit...", sleep_duration)
                            sleep(sleep_duration)
                start_time = time.time()
                request_index += 1

                LOGGER.info(
                    "Pulling 200 tweets from API starting with ID %s and "
                    "ending with ID %s...", since_id, max_id)
                try:
                    if type_ == FAVORITES:
                        loop_statuses = self.GetFavorites(
                            screen_name=username,
                            count=200,
                            since_id=since_id,
                            max_id=max_id,
                            include_entities=True)
                        # 15 requests per 15 minutes.
                        requests_before_sleeps = 15 - 1
                        sleep_time = 60
                    elif type_ == USER:
                        loop_statuses = self.GetUserTimeline(
                            screen_name=username,
                            count=200,
                            since_id=since_id,
                            max_id=max_id)
                        # 300 requests per 15 minutes.
                        sleep_time = 3
                        requests_before_sleeps = 300 - 1
                except twitter.error.TwitterError as e:
                    # If we overran the rate limit, try again.
                    if e.message[0][u'code'] == 88:
                        LOGGER.warning(
                            "Overran rate limit. Sleeping %s seconds in an "
                            "attempt to recover...", sleep_time)
                        request_index = requests_before_sleeps
                        sleep(sleep_time)
                        continue
                    raise
                LOGGER.info(
                    "Found %s tweets this iteration...", len(loop_statuses))
                # Check for "We ran out of tweets via this API" termination
                # condition.
                if not loop_statuses:
                    break
                # Check for early termination condition. We'll kick out if we
                # pass since_id, or if we're pulling user tweets and we've hit
                # this ID previously.
                for loop_status in loop_statuses:
                    status_id = int(loop_status.AsDict()["id"])
                    if ((since_id is not None and status_id >= since_id) or
                            (type_ == "USER" and
                             status_id in existing_tweet_ids)):
                        early_termination = True
                        break

                    # Dump the tweet as a JSON file in case something goes
                    # wrong.
                    tweet_filepath = os.path.join(
                        tweet_storage_path, "%s.json" % status_id)
                    with open(tweet_filepath, 'w') as fptr:
                        json.dump(loop_status.AsDict(), fptr)
                    statuses.append(loop_status)
                    # Capture new max_id
                    if max_id is None or status_id < max_id:
                        max_id = status_id - 1

            # Format things the way we want and handle max_id changes.
            LOGGER.info("Adding %s tweets to DB...", len(statuses))
            existing_tweet_ids = [
                returned_tuple[0]
                for returned_tuple in database.session.query(Tweet.id).all()]
            user = None
            for status in statuses:
                status_dict = status.AsDict()
                status_id = int(status_dict["id"])
                if status_id not in existing_tweet_ids:
                    # Add the user to the DB if needed.
                    # Only really query if we absolutely have to.
                    user_dict = status_dict["user"]
                    user_id = int(user_dict["id"])
                    if user and user.id == user_id:
                        pass
                    else:
                        try:
                            user = database.session.query(TwitterUser). \
                                filter_by(id=user_id).one()
                        except NoResultFound:
                            user = TwitterUser(user_dict)
                            database.session.add(user)

                    # Add the tweet to the DB.
                    media_urls_list = list()
                    if status_dict.get("media"):
                        media_urls_list = [
                            media_dict["media_url_https"]
                            for media_dict in status_dict["media"]
                        ]
                    tweet = Tweet(
                        id=status_id,
                        text=status_dict["text"],
                        in_reply_to_status_id=
                        status_dict.get("in_reply_to_status_id"),
                        created_at=status_dict["created_at"],
                        media_urls_list=media_urls_list,
                    )
                    new_tweets.append(tweet)
                    database.session.add(tweet)

                    # Add the tags
                    if "hashtags" in status_dict:
                        for hashtag_dict in status_dict["hashtags"]:
                            tweet.add_tag(
                                db_session=database.session,
                                tag_name=hashtag_dict["text"])

                    if type_ == FAVORITES:
                        tweet.add_user_favorite(username)
            database.session.commit()

    @classmethod
    def import_tweets_from_csv(cls, database, config, tweet_storage_path,
                               username, csv_filepath, media_storage_path):
        for config_section in config.sections():
            if config_section.startswith("Twitter_%s" % username):
                break
        else:
            LOGGER.error("Username not found.")
            sys.exit(1)
        api = cls(
            consumer_key=config.get(
                section=config_section, option="consumer_key"),
            consumer_secret=config.get(
                section=config_section, option="consumer_secret"),
            access_token_key=config.get(
                section=config_section, option="access_key"),
            access_token_secret=config.get(
                section=config_section, option="access_secret"),
        )
        api.import_from_csv(
            database=database,
            tweet_storage_path=tweet_storage_path,
            csv_filepath=csv_filepath,
            username=username,
            media_storage_path=media_storage_path,
        )

    def import_from_csv(self, database, tweet_storage_path, csv_filepath,
                        username, media_storage_path):
        existing_tweet_ids = database.get_existing_tweet_ids()

        csv_tweets_by_id = dict()
        LOGGER.debug("Scanning CSV for new tweets...")
        with open(csv_filepath) as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                tweet_id = int(row['tweet_id'])
                if tweet_id not in existing_tweet_ids:
                    csv_tweet = CSVTweet(
                        id=tweet_id,
                        username=username,
                        in_reply_to_status_id=row["in_reply_to_status_id"],
                        in_reply_to_user_id=row["in_reply_to_user_id"],
                        timestamp=row["timestamp"],
                        text=row["text"],
                        retweeted_status_id=row["retweeted_status_id"],
                        retweeted_status_user_id=
                        row["retweeted_status_user_id"],
                        retweeted_status_timestamp=
                        row["retweeted_status_timestamp"],
                        expanded_urls=row["expanded_urls"]
                    )
                    csv_tweets_by_id[tweet_id] = csv_tweet

        csv_ids = list(csv_tweets_by_id.keys())
        num_imports = len(csv_ids)
        LOGGER.info(
            "Attempting API import of %s tweets based on CSV file...",
            num_imports)

        # API allows 60 requests per 15 minutes.
        sleep_time = 15
        requests_before_sleeps = 60 - 1

        api_calls = num_imports / 100
        subsequent_api_calls = api_calls - requests_before_sleeps
        if subsequent_api_calls <= 0:
            # Rough estimate, but we basically won't hit the API limit.
            time_to_complete = api_calls * 2
        else:
            time_to_complete = api_calls * 2 + subsequent_api_calls * sleep_time
        LOGGER.info(
            "Estimated time to complete import: %s seconds.", time_to_complete)

        # Set loop starting values
        new_tweets = []
        tweet_index = 0
        request_index = 0
        start_time = -1
        sliced_ids = csv_ids[:100]
        while sliced_ids:

            # Sleep to not hit the rate limit.
            # Twitter rate-limits us. Space this out a bit to avoid a
            # super-long sleep at the end doesn't kill the connection.
            if request_index >= requests_before_sleeps:
                duration = time.time() - start_time
                if duration < sleep_time:
                    if duration < sleep_time:
                        for new_tweet in new_tweets:
                            new_tweet.download_media(
                                db_session=database.session,
                                media_path=media_storage_path)
                        # If we're still too fast, wait however long we need to.
                        duration = time.time() - start_time
                        if duration < sleep_time:
                            sleep_duration = sleep_time - duration
                            LOGGER.info(
                                "Sleeping for %s seconds to avoid hitting "
                                "Twitter's API rate limit...", sleep_duration)
                            sleep(sleep_duration)
            request_index += 1
            start_time = time.time()

            # Perform the import.
            LOGGER.info(
                "Attempting import of id %s to %s of %s...",
                tweet_index + 1, min(tweet_index + 100, num_imports),
                num_imports)
            try:
                statuses = self.LookupStatuses(
                    status_ids=[str(sliced_id) for sliced_id in sliced_ids],
                    trim_user=False,
                    include_entities=True)
                user = None
                for status in statuses:
                    status_dict = status.AsDict()

                    # Dump the tweet as a JSON file in case something goes
                    # wrong. Do none of this if we've passed the since_id
                    # threhold.
                    tweet_filepath = os.path.join(
                        tweet_storage_path, "%s.json" % int(status_dict["id"]))
                    with open(tweet_filepath, 'w') as fptr:
                        json.dump(status_dict, fptr)

                    # Add the user to the DB if needed.
                    # Only really query if we absolutely have to.
                    user_dict = status_dict["user"]
                    user_id = int(user_dict["id"])
                    if user and user.id == user_id:
                        pass
                    else:
                        try:
                            user = database.session.query(TwitterUser). \
                                filter_by(id=user_id).one()
                        except NoResultFound:
                            user = TwitterUser(user_dict)
                            database.session.add(user)

                    status_id = int(status_dict["id"])
                    media_urls_list = list()
                    if status_dict.get("media"):
                        media_urls_list = [
                            media_dict["media_url_https"]
                            for media_dict in status_dict["media"]
                            ]
                    tweet = Tweet(
                        id=status_id,
                        text=status_dict["text"],
                        in_reply_to_status_id=
                        status_dict.get("in_reply_to_status_id"),
                        created_at=status_dict["created_at"],
                        media_urls_list=media_urls_list,
                    )
                    new_tweets.append(tweet)
                    database.session.add(tweet)

                    # Add the tags
                    if "hashtags" in status_dict:
                        for hashtag_dict in status_dict["hashtags"]:
                            tweet.add_tag(
                                db_session=database.session,
                                tag_name=hashtag_dict["text"])

                database.session.commit()

            except TwitterError as e:
                # If we overran the rate limit, try again.
                if e.message[0][u'code'] == 88:
                    LOGGER.warning(
                        "Overran rate limit. Sleeping %s seconds in an "
                        "attempt to recover...", sleep_time)
                    request_index = requests_before_sleeps
                    sleep(sleep_time)
                    continue
                raise
            tweet_index += 100
            sliced_ids = csv_ids[tweet_index:100 + tweet_index]
        database.session.commit()

        LOGGER.info("Parsing out CSV-only tweets...")
        # Refresh existing tweet ID list.
        existing_tweet_ids = database.get_existing_tweet_ids()
        for tweet_id, csv_only_tweet in csv_tweets_by_id.items():
            if tweet_id not in existing_tweet_ids:
                tweet = Tweet.make_from_csvtweet(csv_only_tweet)
                try:
                    user = database.session.query(TwitterUser). \
                        filter_by(screen_name=csv_only_tweet.username).one()
                    user.tweets.append(tweet)
                except NoResultFound:
                    database.session.add(tweet)
        database.session.commit()

    @staticmethod
    def download_media(database, media_storage_path):
        for index, tweet in enumerate(
                database.session.query(Tweet).
                filter(Tweet.files_downloaded.is_(False))):
            tweet.download_media(
                db_session=database.session, media_path=media_storage_path)
            if index % 100 == 0:
                database.session.commit()
        for index, user in enumerate(
                database.session.query(TwitterUser).
                filter(TwitterUser.files_downloaded.is_(False))):
            user.download_media(
                db_session=database.session, media_path=media_storage_path)
            if index % 100 == 0:
                database.session.commit()
        database.session.commit()