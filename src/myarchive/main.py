#!/usr/bin/python3

import argparse
import os
import sys

from myarchive.accounts import LJ_API_ACCOUNTS
from myarchive.db.tag_db.tag_db import TagDB
from myarchive.modules.ljl_ib import LJAPIConnection
from myarchive.modules.twitter_lib import TwitterAPI
from myarchive.modules.shotwell_lib import import_from_shotwell_db
from myarchive.util.logger import myarchive_LOGGER as logger

# from gui import Gtk, MainWindow


from logging import getLogger


LOGGER = getLogger("myarchive")


def main():
    """Starts up the DB connection and GUI."""

    parser = argparse.ArgumentParser(
        description='Manages tagged files.')
    parser.add_argument(
        "--storage-folder",
        action="store",
        default=os.path.join(os.path.expanduser("~"), ".myarchive/"),
        help="Storage folder.")
    parser.add_argument(
        "--import-folder",
        type=str,
        dest="import_folder",
        help="Folder to organize.")
    parser.add_argument(
        '--username',
        action="store",
        default=None,
        help='Accepts a service username.')
    parser.add_argument(
        '--import-tweets-from-api',
        action="store_true",
        default=False,
        help='Downloads user tweets and favorites..')
    parser.add_argument(
        '--import-tweets-from-archive-csv',
        action="store",
        help='Accepts a CSV filepath..')
    parser.add_argument(
        '--import_tweets_from_shotwell_db',
        action="store",
        help='Accepts a shotwell database filepath.')
    parser.add_argument(
        '--import_lj_entries',
        action="store_true",
        default=False,
        help='Imports LJ entries.'
    )
    args = parser.parse_args()
    logger.debug(args)

    # Set up objects used everywhere.
    tag_db = TagDB(
        drivername='sqlite',
        db_name=os.path.join(args.storage_folder, "myarchive.sqlite"))
    tag_db.session.autocommit = False
    media_path = os.path.join(args.storage_folder, "media/")

    if args.import_folder:
        if not os.path.exists(args.import_folder):
            raise Exception("Import folder path does not exist!")
        if not os.path.isdir(args.import_folder):
            raise Exception("Import folder path is not a folder!")

    if args.import_tweets_from_api:
        TwitterAPI.import_tweets_from_api(
            database=tag_db,
            username=args.username,
        )
    if args.import_tweets_from_archive_csv:
        if not args.username:
            logger.error("Username is required for CSV imports!")
            sys.exit(1)
        TwitterAPI.import_tweets_from_csv(
            database=tag_db,
            username=args.username,
            csv_filepath=args.import_tweets_from_archive_csv,
        )
    if args.import_tweets_from_api or args.import_tweets_from_archive_csv:
        # Parse the tweets and download associated media.
        TwitterAPI.parse_tweets(database=tag_db)
        TwitterAPI.download_media(
            database=tag_db, storage_folder=args.storage_folder)

    """
    Shotwell Section
    """

    if args.import_tweets_from_shotwell_db:
        import_from_shotwell_db(
            tag_db=tag_db,
            media_path=media_path,
            sw_database_path=args.import_tweets_from_shotwell_db,
            sw_storage_folder_override=None)

    """
    LIVEJOURNAL SECTION
    """

    if args.import_lj_entries:
        for lj_api_account in LJ_API_ACCOUNTS:
            ljapi = LJAPIConnection(
                db_session=tag_db.session,
                host=lj_api_account.host,
                user_agent=lj_api_account.user_agent,
                username=lj_api_account.username,
                password=lj_api_account.password
            )
            ljapi.download_journals_and_comments(db_session=tag_db.session)

    # MainWindow(tag_db)
    # Gtk.main()

    tag_db.clean_db_and_close()


if __name__ == '__main__':
    main()
