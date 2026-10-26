#!/usr/bin/env python3

import argparse
import sys
import re

def main():
    """
    Lead Python Engineer for PyForge.
    Tool: log_filter
    Description: Reads log lines from stdin and filters them based on a specified log level or regex pattern.
    """
    parser = argparse.ArgumentParser(
        prog='log_filter',
        description='Reads log lines from stdin and filters them based on a specified log level or regex pattern, with an option to output only the match count.'
    )
    
    parser.add_argument(
        '--level',
        type=str,
        required=False,
        default=None,
        help='Filter lines containing this log level (e.g., INFO, WARN, ERROR).'
    )
    
    parser.add_argument(
        '--regex',
        type=str,
        required=False,
        default=None,
        help='Filter lines matching this regular expression.'
    )
    
    parser.add_argument(
        '--count',
        action='store_true',
        help='Output only the count of matching lines.'
    )

    try:
        args = parser.parse_args()
    except SystemExit as e:
        # argparse handles --help and invalid flags automatically
        sys.exit(e.code)

    regex_obj = None
    if args.regex:
        try:
            regex_obj = re.compile(args.regex)
        except re.error as e:
            print(f"Error: Invalid regular expression: {e}", file=sys.stderr)
            sys.exit(1)

    match_count = 0
    
    try:
        # Check if stdin is empty or not available
        if sys.stdin.isatty() and not sys.stdin.seekable():
            # In interactive mode, we wait for input. 
            # If no data is piped, this loop will wait for user input.
            pass

        for line in sys.stdin:
            # Remove trailing newline for processing
            clean_line = line.rstrip('\r\n')
            
            # Filtering logic
            matches_level = True
            if args.level:
                matches_level = args.level in clean_line
            
            matches_regex = True
            if regex_obj:
                matches_regex = bool(regex_obj.search(clean_line))
            
            if matches_level and matches_regex:
                match_count += 1
                if not args.count:
                    # Print the line as it was received (preserving original format)
                    print(clean_line)
                    
        if args.count:
            print(match_count)

    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        sys.exit(130)
    except BrokenPipeError:
        # Handle cases like | head -n 1
        sys.stderr.close()
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)

if __name__ == '__main__':
    main()
