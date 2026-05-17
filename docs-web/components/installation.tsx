import {Tab, Tabs} from "fumadocs-ui/components/tabs";
import {ServerCodeBlock} from 'fumadocs-ui/components/codeblock.rsc';

export function Installation() {
    return (
        <Tabs className={'min-w-lg text-left'} items={['uv', 'pip', 'poetry']}>
            <Tab value="uv"><ServerCodeBlock code={'uv add bedrock-core'} lang={'bash'}/></Tab>
            <Tab value="pip"><ServerCodeBlock code={'pip install bedrock-core'} lang={'bash'}/></Tab>
            <Tab value="poetry"><ServerCodeBlock code={'poetry add bedrock-core'} lang={'bash'}/></Tab>
        </Tabs>
    )
}