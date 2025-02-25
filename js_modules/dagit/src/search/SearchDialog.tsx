import {Colors, Icon, Overlay} from '@blueprintjs/core';
import Fuse from 'fuse.js';
import * as React from 'react';
import {useHistory, useLocation} from 'react-router-dom';
import styled from 'styled-components/macro';

import {ShortcutHandler} from 'src/app/ShortcutHandler';
import {SearchResults} from 'src/search/SearchResults';
import {SearchResult} from 'src/search/types';
import {useRepoSearch} from 'src/search/useRepoSearch';
import {Box} from 'src/ui/Box';
import {Group} from 'src/ui/Group';
import {FontFamily} from 'src/ui/styles';

type State = {
  shown: boolean;
  queryString: string;
  searching: boolean;
  results: Fuse.FuseResult<SearchResult>[];
  highlight: number;
};

type Action =
  | {type: 'show-dialog'}
  | {type: 'hide-dialog'}
  | {type: 'highlight'; highlight: number}
  | {type: 'change-query'; queryString: string}
  | {type: 'complete-search'; results: Fuse.FuseResult<SearchResult>[]};

const reducer = (state: State, action: Action) => {
  switch (action.type) {
    case 'show-dialog':
      return {...state, shown: true};
    case 'hide-dialog':
      return {...state, shown: false, queryString: ''};
    case 'highlight':
      return {...state, highlight: action.highlight};
    case 'change-query':
      return {...state, queryString: action.queryString, searching: true, highlight: 0};
    case 'complete-search':
      return {...state, results: action.results, searching: false};
    default:
      return state;
  }
};

const initialState: State = {
  shown: false,
  queryString: '',
  searching: false,
  results: [],
  highlight: 0,
};

export const SearchDialog: React.FC<{theme: 'dark' | 'light'}> = ({theme}) => {
  const location = useLocation();
  const history = useHistory();
  const performSearch = useRepoSearch();

  const [state, dispatch] = React.useReducer(reducer, initialState);
  const {shown, queryString, results, highlight} = state;

  const numResults = results.length;

  const openSearch = React.useCallback(() => dispatch({type: 'show-dialog'}), []);
  const onChange = React.useCallback((e) => {
    dispatch({type: 'change-query', queryString: e.target.value});
  }, []);

  React.useEffect(() => {
    const results = performSearch(queryString);
    dispatch({type: 'complete-search', results});
  }, [queryString, performSearch]);

  React.useEffect(() => {
    dispatch({type: 'hide-dialog'});
  }, [location.pathname]);

  const onClickResult = React.useCallback(
    (result: Fuse.FuseResult<SearchResult>) => {
      dispatch({type: 'hide-dialog'});
      history.push(result.item.href);
    },
    [history],
  );

  const highlightedResult = results[highlight] || null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    const {key} = e;
    if (key === 'Escape') {
      dispatch({type: 'hide-dialog'});
      return;
    }

    if (!numResults) {
      return;
    }

    const lastResult = numResults - 1;

    switch (key) {
      case 'ArrowUp':
        e.preventDefault();
        dispatch({
          type: 'highlight',
          highlight: highlight === 0 ? lastResult : highlight - 1,
        });
        break;
      case 'ArrowDown':
        e.preventDefault();
        dispatch({
          type: 'highlight',
          highlight: highlight === lastResult ? 0 : highlight + 1,
        });
        break;
      case 'Enter':
        e.preventDefault();
        if (highlightedResult) {
          dispatch({type: 'hide-dialog'});
          history.push(highlightedResult.item.href);
        }
    }
  };

  return (
    <>
      <ShortcutHandler
        onShortcut={() => dispatch({type: 'show-dialog'})}
        shortcutLabel="/"
        shortcutFilter={(e) => e.key === '/'}
      >
        <SearchTrigger onClick={openSearch} $theme={theme}>
          <Box flex={{justifyContent: 'space-between', alignItems: 'center'}}>
            <Group direction="row" alignItems="center" spacing={8}>
              <Icon icon="search" iconSize={11} color={Colors.GRAY3} style={{display: 'block'}} />
              <Placeholder>Search…</Placeholder>
            </Group>
            <SlashShortcut $theme={theme}>{'/'}</SlashShortcut>
          </Box>
        </SearchTrigger>
      </ShortcutHandler>
      <Overlay
        backdropProps={{style: {backgroundColor: 'rgba(0, 0, 0, .35)'}}}
        isOpen={shown}
        onClose={() => dispatch({type: 'hide-dialog'})}
        transitionDuration={100}
      >
        <Container>
          <SearchBox hasQueryString={!!queryString.length}>
            <Icon icon="search" iconSize={18} color={Colors.LIGHT_GRAY3} />
            <SearchInput
              autoFocus
              spellCheck={false}
              onChange={onChange}
              onKeyDown={onKeyDown}
              placeholder="Search pipelines, schedules, sensors…"
              type="text"
              value={queryString}
            />
          </SearchBox>
          <SearchResults
            highlight={highlight}
            queryString={queryString}
            results={results}
            onClickResult={onClickResult}
          />
        </Container>
      </Overlay>
    </>
  );
};

SearchDialog.defaultProps = {
  theme: 'light',
};

const SearchTrigger = styled.button<{$theme: 'dark' | 'light'}>`
  background-color: ${({$theme}) => ($theme === 'light' ? Colors.WHITE : Colors.DARK_GRAY5)};
  border: 1px solid ${({$theme}) => ($theme === 'light' ? Colors.LIGHT_GRAY1 : Colors.GRAY1)};
  border-radius: ${({$theme}) => ($theme === 'light' ? '5px' : '3px')};
  color: ${({$theme}) => ($theme === 'light' ? Colors.GRAY1 : Colors.LIGHT_GRAY3)};
  font-size: 13px;
  font-weight: 400;
  cursor: pointer;
  padding: ${({$theme}) => ($theme === 'light' ? '6px 6px 6px 10px' : '4px 6px 4px 10px')};
  outline: none;
  user-select: none;
  width: 100%;

  :focus {
    border-color: ${({$theme}) => ($theme === 'light' ? Colors.BLUE3 : Colors.LIGHT_GRAY3)};
  }
`;

const Placeholder = styled.div`
  position: relative;
  top: -1px;
`;

const Container = styled.div`
  background-color: ${Colors.WHITE};
  border-radius: 4px;
  box-shadow: 2px 2px 8px rgba(0, 0, 0, 0.1);
  max-height: 60vh;
  left: calc(50% - 300px);
  overflow: hidden;
  width: 600px;
  top: 20vh;
`;

interface SearchBoxProps {
  readonly hasQueryString: boolean;
}

const SearchBox = styled.div<SearchBoxProps>`
  align-items: center;
  border-bottom: ${({hasQueryString}) =>
    hasQueryString ? `1px solid ${Colors.LIGHT_GRAY2}` : 'none'};
  display: flex;
  padding: 12px;
`;

const SearchInput = styled.input`
  border: none;
  color: ${Colors.GRAY1};
  font-family: ${FontFamily.default};
  font-size: 18px;
  margin-left: 8px;
  outline: none;
  width: 100%;

  &::placeholder {
    color: ${Colors.GRAY5};
  }
`;

const SlashShortcut = styled.div<{$theme: 'light' | 'dark'}>`
  background-color: ${({$theme}) => ($theme === 'light' ? Colors.LIGHT_GRAY4 : Colors.DARK_GRAY3)};
  border-radius: 3px;
  color: ${({$theme}) => ($theme === 'light' ? Colors.DARK_GRAY1 : Colors.LIGHT_GRAY4)};
  font-size: 10px;
  font-family: ${FontFamily.monospace};
  padding: 2px 6px;
`;
